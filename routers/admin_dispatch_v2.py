"""Dispatchers v2 — workaround Python module caching en Azure App Service.

Las funciones `dispatch_visit_completed`, `dispatch_day_start_per_driver` y
`_send_post_activation_summary` viven originalmente en
`routers/admin_day_notifications.py` y `routers/twilio_inbound.py`.

Problema: Azure App Service Linux Python 3.11 mantiene bytecode viejo de
esos módulos en memoria del worker uvicorn aunque el filesystem tenga
código nuevo. Después de múltiples deploys + restarts el caching persiste.

Workaround: este archivo es NUEVO (no existía antes en wwwroot). Al
deployarlo, Python lo importa fresh, sin caché. Los call-sites (admin_pilot,
day_state, twilio_inbound) importan desde acá.

Cuando se resuelva el caching del original (slot swap, Python stack swap,
o templates Meta aprobados que vuelven obsoleto el freeform), este módulo
puede consolidarse de vuelta en sus archivos originales.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel

from core.db import get_conn


# ---------------------------------------------------------------------------
# Helpers reusados
# ---------------------------------------------------------------------------

def _mask_phone(phone: str) -> str:
    """Para logs: +56932942337 → +569****2337"""
    if not phone or len(phone) < 8:
        return phone or "(none)"
    return f"{phone[:4]}****{phone[-4:]}"


def _notify_supervisors_v2(
    empresa_id: Optional[int],
    body_text: str,
    *,
    subject: str,
    tracking_id: Optional[str],
    triggered_by: str,
) -> tuple[int, int]:
    """Envía freeform a managers de la empresa + admins Falabella cross-empresa."""
    from routers.notifications import send_whatsapp

    manager_count = 0
    admin_count = 0

    if empresa_id is not None:
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT contact_id, nombre, phone_e164 "
                    "FROM fpoc.empresa_contactos "
                    "WHERE empresa_id = ? AND active = 1 "
                    "  AND LOWER(COALESCE(rol,'')) IN ('jefe','coordinador') "
                    "  AND phone_e164 IS NOT NULL AND opted_in_at IS NOT NULL",
                    empresa_id,
                )
                mgrs = list(cur.fetchall())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dispatch-v2] mgrs empresa {empresa_id} fallo: {e}")
            mgrs = []
        for m in mgrs:
            phone = str(m[2] or "").strip()
            if not phone.startswith("+"):
                continue
            try:
                send_whatsapp(
                    body=body_text,
                    targets=[(None, phone)],
                    subject=subject,
                    tracking_id=tracking_id,
                    triggered_by=f"{triggered_by}_mgr",
                )
                manager_count += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[dispatch-v2] mgr {m[0]} fallo: {e}")

    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT contact_id, nombre, phone_e164 "
                "FROM fpoc.empresa_contactos "
                "WHERE active = 1 "
                "  AND phone_e164 IS NOT NULL AND opted_in_at IS NOT NULL "
                "  AND LOWER(COALESCE(rol,'')) IN ('admin','falabella','falabella_admin','falabella_ops')"
            )
            admins = list(cur.fetchall())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dispatch-v2] admins fallo: {e}")
        admins = []
    for a in admins:
        phone = str(a[2] or "").strip()
        if not phone.startswith("+"):
            continue
        try:
            send_whatsapp(
                body=body_text,
                targets=[(None, phone)],
                subject=subject,
                tracking_id=tracking_id,
                triggered_by=f"{triggered_by}_admin",
            )
            admin_count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dispatch-v2] admin {a[0]} fallo: {e}")

    return manager_count, admin_count


# ---------------------------------------------------------------------------
# 1) Entrega OK (Pieza #1)
# ---------------------------------------------------------------------------

class VisitCompletedV2Response(BaseModel):
    tracking_id: str
    driver_notified: bool
    manager_notified_count: int
    admin_notified_count: int
    completed_count: int
    total_count: int
    detail: str


def dispatch_visit_completed_v2(
    tracking_id: str,
    *,
    triggered_by: str = "visit_completed_v2",
) -> VisitCompletedV2Response:
    """Notif freeform driver + supervisors cuando visita pasa a completed."""
    from routers.notifications import send_whatsapp

    tid = tracking_id.strip()
    today_iso = date.today().isoformat()

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT v.id, v.title, v.comuna, v.patente_falsa, v.planned_date "
            "FROM fpoc.simpli_visits v "
            "WHERE CAST(v.id AS VARCHAR(32)) = ?",
            tid,
        )
        v = cur.fetchone()
    if v is None:
        raise HTTPException(404, f"Visita {tid} no existe")

    cliente = str(v[1] or "—")
    comuna = str(v[2] or "")
    patente = int(v[3]) if v[3] is not None else None
    planned_date = str(v[4]) if v[4] else today_iso
    cliente_label = f"{cliente}" + (f" ({comuna})" if comuna else "")

    if patente is None:
        raise HTTPException(409, f"Visita {tid} sin patente_falsa")

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT d.driver_id, d.name, d.phone_e164, d.notify_whatsapp, "
            "       d.opted_in_at, d.empresa_id, e.nombre "
            "FROM fpoc.drivers d "
            "LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id "
            "WHERE d.vehicle_id = ? AND d.active = 1 "
            "ORDER BY d.opted_in_at DESC",
            patente,
        )
        d = cur.fetchone()
    if d is None:
        raise HTTPException(409, f"No hay driver activo para patente {patente}")

    driver_id = str(d[0]) if d[0] else "?"
    driver_name = str(d[1] or "—")
    driver_phone = str(d[2] or "").strip()
    driver_notify = bool(d[3]) if d[3] is not None else False
    driver_optin = d[4]
    empresa_id = int(d[5]) if d[5] is not None else None
    empresa_nombre = str(d[6] or "—")

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT SUM(CASE WHEN LOWER(status)='completed' THEN 1 ELSE 0 END), "
            "       COUNT(*) "
            "FROM fpoc.simpli_visits "
            "WHERE planned_date = ? AND patente_falsa = ?",
            planned_date, patente,
        )
        r = cur.fetchone()
    completed_count = int(r[0] or 0)
    total_count = int(r[1] or 0)
    pending_count = max(0, total_count - completed_count)
    hora_str = datetime.now().strftime("%H:%M")

    driver_notified = False
    _filtro = (
        f"phone_starts_plus={driver_phone.startswith('+')} "
        f"notify={driver_notify} optin_not_none={driver_optin is not None}"
    )
    logger.info(f"[dispatch-v2/visit-completed] TID={tid} filtro: {_filtro}")

    if driver_phone.startswith("+") and driver_notify and driver_optin is not None:
        body_driver = (
            f"✅ *Entrega OK*: {cliente_label}\n"
            f"Te quedan *{pending_count}* pendientes hoy.\n"
            f"Mandá 'menu' para ver tu ruta restante."
        )
        try:
            res = send_whatsapp(
                body=body_driver,
                targets=[(None, driver_phone)],
                subject=f"Entrega OK · TID:{tid}",
                tracking_id=tid,
                triggered_by=triggered_by,
            )
            first = res.results[0] if res.results else None
            logger.info(
                f"[dispatch-v2/visit-completed] TID={tid} send_whatsapp: "
                f"status={first.status if first else 'no-result'} "
                f"sid={first.twilio_sid if first else None}"
            )
            # Twilio devuelve 'queued' inmediatamente y 'sent'/'delivered'
            # despues. Cualquier estado no-error cuenta como notificado.
            ok_states = {"sent", "queued", "delivered", "dry_run", "pending", "scheduled"}
            if first and (first.status in ok_states or (first.twilio_sid and not first.error)):
                driver_notified = True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[dispatch-v2/visit-completed] TID={tid} exception: {e}")

    body_supervisors = (
        f"✅ *{driver_name}* entregó \"{cliente_label}\" a las {hora_str}.\n"
        f"Empresa: *{empresa_nombre}* — Progreso: {completed_count}/{total_count} OK."
    )
    manager_notified, admin_notified = _notify_supervisors_v2(
        empresa_id, body_supervisors,
        subject=f"Entrega OK · {empresa_nombre}",
        tracking_id=tid,
        triggered_by=triggered_by,
    )

    detail = (
        f"driver_notified={driver_notified} mgrs={manager_notified} "
        f"admins={admin_notified} ({completed_count}/{total_count}) [{_filtro}]"
    )
    logger.info(f"[dispatch-v2/visit-completed] TID={tid} {detail}")

    return VisitCompletedV2Response(
        tracking_id=tid,
        driver_notified=driver_notified,
        manager_notified_count=manager_notified,
        admin_notified_count=admin_notified,
        completed_count=completed_count,
        total_count=total_count,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 2) Inicio del día — Buenos días automático
# ---------------------------------------------------------------------------

# Cache in-memory para evitar broadcast duplicado por fecha (se resetea cuando
# day-state vuelve a BORRADOR vía la función reset_day_start_cache_v2).
_day_start_sent_v2: dict[str, bool] = {}


def reset_day_start_cache_v2(fecha: str) -> None:
    _day_start_sent_v2.pop(fecha, None)


def dispatch_day_start_per_driver_v2(
    fecha: str,
    *,
    triggered_by: str = "day_start_auto_v2",
) -> dict:
    """Cuando day-state pasa a EN_CURSO, a cada driver opted-in con visitas
    pending hoy le manda "Buenos días! Hoy tenés N visitas..."
    Idempotente por (fecha, started_at) — el started_at cambia cada vez que
    se reabre el día (vía reset + transition), invalidando el cache.
    """
    from routers.notifications import send_whatsapp

    # Cache key incluye started_at: si el día se re-arrancó, started_at cambió
    # y la entry vieja del cache no aplica. Esto evita el bug multi-worker
    # donde un worker tenía el cache marcado y otro lo había limpiado.
    cache_key = fecha
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT started_at FROM fpoc.planificacion_imports WHERE fecha = ?",
                fecha,
            )
            r = cur.fetchone()
            if r and r[0]:
                cache_key = f"{fecha}@{r[0]}"
    except Exception:  # noqa: BLE001
        pass

    if _day_start_sent_v2.get(cache_key):
        logger.info(f"[dispatch-v2/day-start] {cache_key} ya broadcast'd, skip")
        return {"drivers_notified": 0, "skipped": "already_sent"}

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT d.driver_id, d.name, d.phone_e164, d.vehicle_id,
                   COUNT(v.id) AS visitas, MIN(v.current_eta_cl) AS first_eta
            FROM fpoc.drivers d
            JOIN fpoc.simpli_visits v ON v.patente_falsa = d.vehicle_id
            WHERE v.planned_date = ?
              AND LOWER(v.status) = 'pending'
              AND d.active = 1
              AND d.phone_e164 IS NOT NULL
              AND d.opted_in_at IS NOT NULL
              AND d.notify_whatsapp = 1
            GROUP BY d.driver_id, d.name, d.phone_e164, d.vehicle_id
            """,
            fecha,
        )
        rows = list(cur.fetchall())

    drivers_notified = 0
    for r in rows:
        phone = str(r[2] or "").strip()
        if not phone.startswith("+"):
            continue
        first_name = str(r[1] or "—").split(" ")[0]
        vehicle_id = int(r[3])
        visitas = int(r[4] or 0)
        first_eta_raw = r[5]
        first_eta = (
            first_eta_raw.strftime("%H:%M") if hasattr(first_eta_raw, "strftime")
            else (str(first_eta_raw)[11:16] if first_eta_raw else "—")
        )

        first_cliente = "—"
        try:
            with get_conn() as cn2:
                cur2 = cn2.cursor()
                cur2.execute(
                    "SELECT TOP 1 title FROM fpoc.simpli_visits "
                    "WHERE planned_date=? AND patente_falsa=? AND LOWER(status)='pending' "
                    "ORDER BY current_eta_cl ASC",
                    fecha, vehicle_id,
                )
                rc = cur2.fetchone()
                if rc:
                    first_cliente = str(rc[0] or "—")
        except Exception:  # noqa: BLE001
            pass

        body = (
            f"☀️ ¡Buenos días, *{first_name}*!\n\n"
            f"Tu jornada arranca. Hoy tenés *{visitas} visitas* programadas.\n"
            f"📍 Primera: *{first_cliente}* a las *{first_eta}*.\n\n"
            f"Mandá *'menu'* para opciones, *'1'* para tu ruta completa, "
            f"*'2'* para próxima visita."
        )
        try:
            send_whatsapp(
                body=body,
                targets=[(None, phone)],
                subject=f"Inicio de jornada · {fecha}",
                triggered_by=triggered_by,
            )
            drivers_notified += 1
            logger.info(f"[dispatch-v2/day-start] sent to {_mask_phone(phone)}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dispatch-v2/day-start] driver {r[0]} fallo: {e}")

    _day_start_sent_v2[fecha] = True
    logger.info(f"[dispatch-v2/day-start] {fecha} drivers_notified={drivers_notified}")
    return {"drivers_notified": drivers_notified, "candidates": len(rows)}


# ---------------------------------------------------------------------------
# 3) Welcome post-ACTIVAR
# ---------------------------------------------------------------------------

def send_post_activation_summary_v2(
    *,
    role: str,  # 'driver' | 'contact' | 'user'
    row_id: str,
    phone: str,
    first_name: str,
    sender_to: Optional[str] = None,
) -> None:
    """Después de ACTIVAR exitoso (template vd_cuenta_activada salió), envía
    un freeform con resumen según rol. Best-effort: errores se loggean sin
    propagar.
    """
    from routers.notifications import send_whatsapp

    today = date.today().isoformat()
    body = None

    try:
        if role == "driver":
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT TOP 3 v.title, v.comuna, v.current_eta_cl "
                    "FROM fpoc.simpli_visits v "
                    "JOIN fpoc.drivers d ON d.vehicle_id = v.patente_falsa "
                    "WHERE d.driver_id = ? AND v.planned_date = ? "
                    "  AND LOWER(v.status) = 'pending' "
                    "ORDER BY v.current_eta_cl ASC",
                    row_id, today,
                )
                rows = cur.fetchall()
            if rows:
                first = rows[0]
                eta = (
                    first[2].strftime("%H:%M") if hasattr(first[2], "strftime")
                    else str(first[2])[11:16]
                )
                cliente = str(first[0] or "—")
                comuna = str(first[1] or "")
                comuna_part = f" ({comuna})" if comuna else ""
                body = (
                    f"¡Hola *{first_name}*! 👋\n\n"
                    f"Tu ruta de hoy tiene *{len(rows)}+ visitas pendientes*.\n"
                    f"📍 *Primera*: {cliente}{comuna_part} a las *{eta}*.\n\n"
                    f"Mandá *'menu'* para opciones, *'1'* para ruta completa, "
                    f"*'2'* próxima."
                )
            else:
                body = (
                    f"¡Hola *{first_name}*! 👋\n\n"
                    f"Tu cuenta está activada. Cuando el equipo cargue las "
                    f"visitas del día te aviso. Mandá *'menu'* cuando quieras."
                )

        elif role == "contact":
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT c.nombre, c.rol, e.empresa_id, COALESCE(e.nombre,'') "
                    "FROM fpoc.empresa_contactos c "
                    "LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = c.empresa_id "
                    "WHERE c.contact_id = ?",
                    int(row_id),
                )
                c = cur.fetchone()
            if not c:
                return
            empresa_id = c[2]
            empresa_nombre = str(c[3] or "tu empresa")
            rol = str(c[1] or "manager").lower()

            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT COUNT(DISTINCT v.patente_falsa), COUNT(*) "
                    "FROM fpoc.simpli_visits v "
                    "WHERE v.planned_date = ? AND v.empresa_falsa = ?",
                    today, empresa_id,
                )
                r = cur.fetchone()
            drivers = int(r[0] or 0)
            visitas = int(r[1] or 0)

            body = (
                f"¡Hola *{first_name}*! 👋\n\n"
                f"Sos *{rol}* de *{empresa_nombre}*.\n"
                f"Hoy: *{drivers} drivers activos · {visitas} visitas* programadas.\n\n"
                f"Vas a recibir alertas cuando un driver tenga atraso, complete "
                f"entregas, o cuando admin Falabella intervenga un folio.\n\n"
                f"Mandá *'menu'* para consultar KPIs, alertas o buscar visitas."
            )

        elif role == "user":
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute("SELECT role FROM fpoc.users WHERE user_id = ?", int(row_id))
                r = cur.fetchone()
                rol = str(r[0] if r else "ops")
                cur.execute(
                    "SELECT COUNT(DISTINCT empresa_falsa), COUNT(DISTINCT patente_falsa), COUNT(*) "
                    "FROM fpoc.simpli_visits WHERE planned_date = ?",
                    today,
                )
                r = cur.fetchone()
            empresas = int(r[0] or 0)
            drivers = int(r[1] or 0)
            visitas = int(r[2] or 0)
            role_label = "Falabella Admin" if rol == "falabella_admin" else "Falabella Ops"
            body = (
                f"¡Hola *{first_name}*! 👋\n\n"
                f"Bienvenido al equipo *{role_label}*.\n"
                f"Hoy globalmente: *{empresas} empresas · {drivers} drivers · "
                f"{visitas} visitas*.\n\n"
                f"Mandá *'menu'* para KPIs globales, alertas críticas, "
                f"intervenciones, búsqueda por TRK."
            )

        if body:
            send_whatsapp(
                body=body,
                targets=[(None, phone)],
                subject="Bienvenida + resumen del día",
                triggered_by="post_activation_welcome_v2",
                from_number=sender_to,
            )
            logger.info(
                f"[dispatch-v2/welcome] sent to {_mask_phone(phone)} role={role}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dispatch-v2/welcome] fallo: {e}")

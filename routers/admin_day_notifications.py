"""Endpoints administrativos para disparar notificaciones operativas E2E.

Provee dos triggers manuales pensados para QA / demo / arranque de jornada:

  POST /api/admin/notify-day-start   — broadcast "inicio de jornada" a todos
                                       los drivers opted-in con visitas hoy.
  POST /api/admin/notify-eta-breach  — alerta de atraso ETA para 1 visita
                                       puntual (driver responde con la causa).

Ambos usan el template Meta-approved `vd_alerta_motivo_v2`
(env `TWILIO_CONTENT_SID_ALERTA_MOTIVO`, fallback hardcoded al SID
`HX6821f9cad06ce1980bee5ad410006e43`).

Auth: solo `falabella_admin` y `falabella_ops` (gate local
`_require_admin_or_ops`, mismo patrón que `routers/admin_invitations.py`).

El endpoint de ETA-breach además persiste el `tracking_id` alertado en
`fpoc_whatsapp_sessions.context.last_alerted_tid` para el phone del driver,
de modo que el agente LLM pueda recuperar el contexto cuando el driver
responda con su justificación en lenguaje natural (ver
`sims/llm_agent.py::_build_system_prompt`).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import get_conn


router = APIRouter(prefix="/api/admin", tags=["admin-day-notifications"])


# ---------------------------------------------------------------------------
# Constantes / template
# ---------------------------------------------------------------------------

# vd_alerta_motivo_v2 — 6 vars (severidad, motivo, vehiculo, conductor,
# cliente, comentario). Aprobado por Meta. Centralizado en
# `core.twilio_templates` (CR fixes-qa M7).


def _alerta_motivo_sid() -> str:
    from core.twilio_templates import alerta_motivo_sid
    return alerta_motivo_sid()


# ---------------------------------------------------------------------------
# Auth guard local (mismo patrón que admin_invitations.py)
# ---------------------------------------------------------------------------

def _require_admin_or_ops(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_falabella:
        raise HTTPException(403, "Requiere rol falabella_admin o falabella_ops")
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class NotifyDayStartRequest(BaseModel):
    fecha: Optional[str] = Field(
        default=None,
        description="Fecha planificada (YYYY-MM-DD). Default: hoy.",
    )
    empresa_id: Optional[int] = Field(
        default=None,
        description="Si se indica, filtra a esa empresa. None = todas.",
    )
    dry_run: bool = Field(
        default=False,
        description="Si true, no envía WhatsApp; solo lista los drivers candidatos.",
    )


class NotifyDayStartDriverItem(BaseModel):
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    phone: Optional[str] = None
    vehicle: str
    patente_falsa: int
    pending: int
    completed: int
    total: int
    status: str  # 'sent' | 'skipped' | 'dry_run' | 'failed'
    skipped_reason: Optional[str] = None
    twilio_sid: Optional[str] = None
    error: Optional[str] = None


class NotifyDayStartResponse(BaseModel):
    fecha: str
    empresa_id: Optional[int] = None
    dry_run: bool
    notifications_sent: int
    notifications_skipped: int
    notifications_failed: int
    skipped_reasons: list[str]
    drivers: list[NotifyDayStartDriverItem]


class NotifyEtaBreachRequest(BaseModel):
    tracking_id: str = Field(min_length=1, max_length=64)


class NotifyEtaBreachResponse(BaseModel):
    tracking_id: str
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    vehicle: str
    twilio_sid: Optional[str] = None
    status: str  # 'queued' | 'sent' | 'dry_run' | 'failed'
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return date.today().isoformat()


def _set_last_alerted_tid(phone: str, tracking_id: str) -> None:
    """Persiste el tracking_id de la última alerta enviada al driver en
    `fpoc_whatsapp_sessions.context`. El LLM agent lo recupera cuando el
    driver responde con texto natural (sin tipear el tracking_id) para
    invocar la tool `report_motivo` con contexto."""
    try:
        # Import lazy: whatsapp_agent depende de loguru/db en module-load y
        # algunos hot reloads pueden tener side-effects. Mantenerlo local
        # también evita ciclos con routers que importan este módulo.
        from sims.whatsapp_agent import Session  # noqa: WPS433

        sess = Session.load(phone)
        if not sess.context:
            sess.context = {}
        sess.context["last_alerted_tid"] = tracking_id
        sess.context["last_alerted_at"] = datetime.utcnow().isoformat()
        sess.save()
    except Exception as e:  # noqa: BLE001
        # No es crítico: si falla, igual el broadcast se envía. El driver
        # tendrá que tipear el tracking_id explícito.
        logger.warning(
            f"[admin_day_notifications] _set_last_alerted_tid({phone}) failed: {e}"
        )


# ---------------------------------------------------------------------------
# Endpoint 1: notify-day-start
# ---------------------------------------------------------------------------

@router.post("/notify-day-start", response_model=NotifyDayStartResponse)
def notify_day_start(
    req: NotifyDayStartRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> NotifyDayStartResponse:
    """Dispara un broadcast de inicio de jornada a todos los drivers opted-in
    de la(s) empresa(s) que tienen visitas hoy. Usa template aprobado
    `vd_alerta_motivo_v2` con `severidad=INFO` y `motivo=INICIO DE JORNADA`.
    """
    from routers.comments import _sanitize_template_var
    from routers.notifications import send_whatsapp

    fecha = req.fecha or _today_iso()

    # Validar empresa_id antes de gastar queries — un operador con typo
    # (ej. {"empresa_id": 99999}) recibía 200 con drivers:[] y se creía OK.
    if req.empresa_id is not None:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT 1 FROM fpoc.empresas_transporte WHERE empresa_id = ?",
                req.empresa_id,
            )
            if not cur.fetchone():
                raise HTTPException(404, f"Empresa {req.empresa_id} no existe")

    # 1) Agrupar visitas del día por patente_falsa (proxy vehicle_id).
    where_extra = ""
    params: list = [fecha]
    if req.empresa_id is not None:
        where_extra = " AND empresa_falsa = ?"
        params.append(req.empresa_id)

    # where_extra es un literal server-controlled (string vacío o " AND
    # empresa_falsa = ?"), todos los valores van por placeholders ?.
    sql_groups = (
        "SELECT patente_falsa, "
        "       SUM(CASE WHEN LOWER(status) = 'pending' THEN 1 ELSE 0 END) AS pending, "
        "       SUM(CASE WHEN LOWER(status) = 'completed' THEN 1 ELSE 0 END) AS completed, "
        "       COUNT(*) AS total "
        f"FROM fpoc.simpli_visits WHERE planned_date = ?{where_extra} "  # nosec B608
        "GROUP BY patente_falsa"
    )

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(sql_groups, *params)
        groups = cur.fetchall()

    drivers_out: list[NotifyDayStartDriverItem] = []
    sent_n = 0
    skipped_n = 0
    failed_n = 0
    skipped_reasons: list[str] = []

    for g in groups:
        patente = int(g[0]) if g[0] is not None else None
        pending = int(g[1] or 0)
        completed = int(g[2] or 0)
        total = int(g[3] or 0)
        if patente is None:
            continue
        vehicle_label = f"PAT-{patente}"

        # Solo broadcast si hay pendientes hoy.
        if pending <= 0:
            reason = f"{vehicle_label}: sin visitas pendientes (pending=0)"
            skipped_reasons.append(reason)
            skipped_n += 1
            drivers_out.append(NotifyDayStartDriverItem(
                vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="skipped", skipped_reason="no_pending_visits",
            ))
            continue

        # 2) Resolver driver opted-in para esa patente.
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT driver_id, name, phone_e164, notify_whatsapp, opted_in_at, active "
                "FROM fpoc.drivers "
                "WHERE vehicle_id = ? AND active = 1 "
                "ORDER BY opted_in_at DESC "
                "LIMIT 1",
                patente,
            )
            d_row = cur.fetchone()

        if d_row is None:
            reason = f"{vehicle_label}: no hay driver activo asociado"
            skipped_reasons.append(reason)
            skipped_n += 1
            drivers_out.append(NotifyDayStartDriverItem(
                vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="skipped", skipped_reason="no_driver",
            ))
            continue

        driver_id = str(d_row[0]) if d_row[0] else None
        driver_name = str(d_row[1]) if d_row[1] else "—"
        phone = str(d_row[2]).strip() if d_row[2] else ""
        notify_flag = bool(d_row[3]) if d_row[3] is not None else False
        opted_in_at = d_row[4]

        if not phone or not phone.startswith("+"):
            reason = f"Driver {driver_id or vehicle_label} sin phone E.164 válido"
            skipped_reasons.append(reason)
            skipped_n += 1
            drivers_out.append(NotifyDayStartDriverItem(
                driver_id=driver_id, driver_name=driver_name,
                phone=phone or None, vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="skipped", skipped_reason="no_phone",
            ))
            continue

        if not notify_flag:
            reason = f"Driver {driver_id} notify_whatsapp=0"
            skipped_reasons.append(reason)
            skipped_n += 1
            drivers_out.append(NotifyDayStartDriverItem(
                driver_id=driver_id, driver_name=driver_name,
                phone=phone, vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="skipped", skipped_reason="notify_whatsapp_disabled",
            ))
            continue

        if opted_in_at is None:
            reason = f"Driver {driver_id} sin opt-in confirmado"
            skipped_reasons.append(reason)
            skipped_n += 1
            drivers_out.append(NotifyDayStartDriverItem(
                driver_id=driver_id, driver_name=driver_name,
                phone=phone, vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="skipped", skipped_reason="no_opt_in",
            ))
            continue

        # Dry-run: no enviamos, solo listamos como candidato.
        if req.dry_run:
            drivers_out.append(NotifyDayStartDriverItem(
                driver_id=driver_id, driver_name=driver_name,
                phone=phone, vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="dry_run",
            ))
            continue

        # 3) Resolver primera visita pendiente para mostrarla como "primer destino".
        first_title = "—"
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT title FROM fpoc.simpli_visits "
                    "WHERE planned_date = ? AND patente_falsa = ? "
                    "  AND LOWER(status) = 'pending' "
                    "ORDER BY \"order\" ASC, id ASC "
                    "LIMIT 1",
                    fecha, patente,
                )
                r = cur.fetchone()
                if r and r[0]:
                    first_title = str(r[0])
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[notify-day-start] first_title lookup falló para {vehicle_label}: {e}"
            )

        # 4) Construir variables del template y enviar.
        content_variables = {
            "1": _sanitize_template_var("INFO") or "INFO",
            "2": _sanitize_template_var("INICIO DE JORNADA") or "INICIO DE JORNADA",
            "3": _sanitize_template_var(vehicle_label) or "—",
            "4": _sanitize_template_var(driver_name) or "—",
            "5": _sanitize_template_var(f"Primer destino: {first_title}") or "—",
            "6": _sanitize_template_var(
                f"Tu jornada operativa comenzo. Tenes {pending} visitas pendientes hoy. "
                f"Responde 'menu' para ver opciones o 'ruta' para detalles.",
                max_len=200,
            ) or "—",
        }
        subject_line = f"Inicio jornada {fecha} · {vehicle_label}"

        try:
            res = send_whatsapp(
                content_sid=_alerta_motivo_sid(),
                content_variables=content_variables,
                targets=[(None, phone)],
                subject=subject_line,
                triggered_by="day_start_broadcast",
            )
            first = res.results[0] if res.results else None
            if first and first.status in ("sent", "dry_run"):
                sent_n += 1
                drivers_out.append(NotifyDayStartDriverItem(
                    driver_id=driver_id, driver_name=driver_name,
                    phone=phone, vehicle=vehicle_label,
                    patente_falsa=patente,
                    pending=pending, completed=completed, total=total,
                    status=first.status,
                    twilio_sid=first.twilio_sid,
                ))
            else:
                failed_n += 1
                drivers_out.append(NotifyDayStartDriverItem(
                    driver_id=driver_id, driver_name=driver_name,
                    phone=phone, vehicle=vehicle_label,
                    patente_falsa=patente,
                    pending=pending, completed=completed, total=total,
                    status="failed",
                    error=(first.error if first else "unknown"),
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[notify-day-start] envío falló para {vehicle_label}: {e}"
            )
            failed_n += 1
            drivers_out.append(NotifyDayStartDriverItem(
                driver_id=driver_id, driver_name=driver_name,
                phone=phone, vehicle=vehicle_label,
                patente_falsa=patente,
                pending=pending, completed=completed, total=total,
                status="failed", error=str(e)[:300],
            ))

    return NotifyDayStartResponse(
        fecha=fecha,
        empresa_id=req.empresa_id,
        dry_run=req.dry_run,
        notifications_sent=sent_n,
        notifications_skipped=skipped_n,
        notifications_failed=failed_n,
        skipped_reasons=skipped_reasons,
        drivers=drivers_out,
    )


# ---------------------------------------------------------------------------
# Endpoint 2: notify-eta-breach
# ---------------------------------------------------------------------------

def dispatch_eta_breach(
    tracking_id: str,
    *,
    triggered_by: str = "eta_breach_manual",
) -> NotifyEtaBreachResponse:
    """Logica reusable de notify-eta-breach.

    Llamada directa desde:
      - el endpoint POST /api/admin/notify-eta-breach (manual)
      - el endpoint POST /api/admin/pilot/simulate-event (event=delay)
      - sims.eta_breach_cron (chequeo automatico cada 5 min)

    Raises:
      HTTPException — si la visita no existe o el driver no esta apto para
      recibir el WhatsApp.
    """
    from routers.comments import _sanitize_template_var
    from routers.notifications import send_whatsapp

    tid = tracking_id.strip()

    # 1) SELECT visita.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT id, title, status, current_eta_cl, patente_falsa, "
            "       address, driver_name, comuna "
            "FROM fpoc.simpli_visits WHERE CAST(id AS VARCHAR(32)) = ?",
            tid,
        )
        v_row = cur.fetchone()

    if v_row is None:
        raise HTTPException(404, f"Visita {tid} no existe")

    title = str(v_row[1] or "")
    eta_raw = str(v_row[3] or "")
    # current_eta_cl viene 'YYYY-MM-DD HH:MM:SS' — extraer HH:MM si aplica.
    eta_short = eta_raw.split(" ")[1][:5] if " " in eta_raw else eta_raw[:5]
    patente = int(v_row[4]) if v_row[4] is not None else None
    visit_driver_name = str(v_row[6] or "")
    comuna = str(v_row[7] or "")
    vehicle_label = f"PAT-{patente}" if patente is not None else "—"

    if patente is None:
        raise HTTPException(409, f"Visita {tid} sin patente_falsa asignada")

    # 2) Resolver driver activo para esa patente + empresa.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT d.driver_id, d.name, d.phone_e164, d.notify_whatsapp, d.opted_in_at, "
            "       d.empresa_id, e.nombre AS empresa_nombre "
            "FROM fpoc.drivers d "
            "LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id "
            "WHERE d.vehicle_id = ? AND d.active = 1 "
            "ORDER BY d.opted_in_at DESC",
            patente,
        )
        d_row = cur.fetchone()

    if d_row is None:
        raise HTTPException(
            409, f"No hay driver activo asociado a patente {patente}"
        )

    driver_id = str(d_row[0]) if d_row[0] else None
    driver_name = str(d_row[1]) if d_row[1] else visit_driver_name or "—"
    phone = str(d_row[2]).strip() if d_row[2] else ""
    notify_flag = bool(d_row[3]) if d_row[3] is not None else False
    opted_in_at = d_row[4]
    driver_empresa_id = int(d_row[5]) if d_row[5] is not None else None
    driver_empresa_nombre = str(d_row[6] or "—")

    if not phone or not phone.startswith("+"):
        raise HTTPException(
            409, f"Driver {driver_id or '?'} sin phone E.164 válido"
        )
    if not notify_flag:
        raise HTTPException(409, f"Driver {driver_id} con notify_whatsapp=0")
    if opted_in_at is None:
        raise HTTPException(409, f"Driver {driver_id} sin opt-in confirmado")

    # 3) Variables del template + send.
    # Ventana planeada: simpli_visits NO tiene window_end discreto en DB
    # (ver sims/_visits_db.py docstring), así que reportamos "planeada".
    cliente_part = f"{title}" + (f" ({comuna})" if comuna else "")
    comentario = (
        f"TID:{tid} con ETA {eta_short or '—'} fuera de ventana planeada. "
        f"Responde con la causa del atraso (ej: 'siniestro en calle', "
        f"'sin moradores', etc)."
    )

    content_variables = {
        "1": _sanitize_template_var("HIGH") or "HIGH",
        "2": _sanitize_template_var("POSIBLE ATRASO ETA") or "POSIBLE ATRASO ETA",
        "3": _sanitize_template_var(vehicle_label) or "—",
        "4": _sanitize_template_var(driver_name) or "—",
        "5": _sanitize_template_var(cliente_part) or "—",
        "6": _sanitize_template_var(comentario, max_len=200) or "—",
    }
    subject_line = f"Atraso ETA · TID:{tid} · {vehicle_label}"

    # 4) IMPORTANTE: persistir tracking_id en sesión WhatsApp del driver
    # ANTES de enviar (así si el driver responde inmediatamente, el LLM
    # ya tiene el contexto).
    _set_last_alerted_tid(phone, tid)

    twilio_sid: Optional[str] = None
    error: Optional[str] = None
    status = "failed"
    try:
        res = send_whatsapp(
            content_sid=_alerta_motivo_sid(),
            content_variables=content_variables,
            targets=[(None, phone)],
            subject=subject_line,
            tracking_id=tid,
            triggered_by=triggered_by,
        )
        first = res.results[0] if res.results else None
        if first:
            status = first.status if first.status != "sent" else "queued"
            twilio_sid = first.twilio_sid
            error = first.error
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[notify-eta-breach] envío falló para TID={tid}: {e}")
        error = str(e)[:300]
        status = "failed"

    # 5) Broadcast a supervisors (managers + admins). Freeform, no template.
    body_sup = (
        f"🚨 *Atraso* · {driver_empresa_nombre}\n"
        f"{driver_name} ({vehicle_label}) demorado en \"{cliente_part}\".\n"
        f"ETA reportada: {eta_short or '—'}. Bot le pidió motivo al chofer."
    )
    try:
        _notify_supervisors(
            driver_empresa_id, body_sup,
            subject=f"Atraso ETA · {driver_empresa_nombre}",
            tracking_id=tid,
            triggered_by=triggered_by,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[notify-eta-breach] supervisors fallo TID={tid}: {e}")

    return NotifyEtaBreachResponse(
        tracking_id=tid,
        driver_id=driver_id,
        driver_name=driver_name,
        driver_phone=phone,
        vehicle=vehicle_label,
        twilio_sid=twilio_sid,
        status=status,
        error=error,
    )


@router.post("/notify-eta-breach", response_model=NotifyEtaBreachResponse)
def notify_eta_breach(
    req: NotifyEtaBreachRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> NotifyEtaBreachResponse:
    """Manual trigger para disparar alerta de atraso ETA en una visita
    específica (uso QA / demo). Persiste el `tracking_id` en la sesión
    WhatsApp del driver para que el LLM agent pueda enlazar la respuesta
    libre del driver con el `report_motivo` correspondiente.

    La logica real vive en `dispatch_eta_breach()` para que el cron
    `sims.eta_breach_cron` y el `/api/admin/pilot/simulate-event` la puedan
    reusar sin duplicar codigo.
    """
    return dispatch_eta_breach(req.tracking_id, triggered_by="eta_breach_manual")


# ---------------------------------------------------------------------------
# Dispatch visit completed (CR-entrega-ok)
# ---------------------------------------------------------------------------

class VisitCompletedResponse(BaseModel):
    tracking_id: str
    driver_notified: bool
    manager_notified_count: int
    admin_notified_count: int
    completed_count: int
    total_count: int
    detail: str


class DayCloseSummaryResponse(BaseModel):
    fecha: str
    drivers_notified: int
    manager_messages_sent: int
    admin_messages_sent: int
    detail: str


def _notify_supervisors(
    empresa_id: Optional[int],
    body_text: str,
    *,
    subject: str,
    tracking_id: Optional[str],
    triggered_by: str,
) -> tuple[int, int]:
    """Envía freeform body_text a:
      - managers de la empresa (rol jefe/coordinador, opted-in)
      - admin Falabella cross-empresa (rol admin/falabella/...)

    Devuelve (manager_count, admin_count) — cuántos envíos OK.
    Errores por receptor se loggean como WARNING y no propagan.
    """
    from routers.notifications import send_whatsapp

    manager_count = 0
    admin_count = 0

    # 1) Managers de la empresa.
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
            logger.warning(f"[notify-supervisors] mgrs empresa {empresa_id} fallo: {e}")
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
                logger.warning(f"[notify-supervisors] mgr {m[0]} fallo: {e}")

    # 2) Admins Falabella cross-empresa.
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
        logger.warning(f"[notify-supervisors] admins fallo: {e}")
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
            logger.warning(f"[notify-supervisors] admin {a[0]} fallo: {e}")

    return manager_count, admin_count


def dispatch_day_close_summary(
    fecha: str,
    *,
    triggered_by: str = "day_close_manual",
) -> DayCloseSummaryResponse:
    """Cuando se cierra el día (BORRADOR/CERRADO transition), envía resumen
    por WhatsApp a:
      - Cada driver opted-in: stats personales (X/Y OK, atrasos, motivos).
      - Cada manager de empresa: stats agregadas de su flota.
      - Cada admin Falabella cross-empresa: stats globales.

    Freeform body. Si el receptor no tiene ventana 24h abierta, send_whatsapp
    fallará pero se loggea como warning sin bloquear al resto.
    """
    from routers.notifications import send_whatsapp

    drivers_notified = 0
    manager_messages = 0
    admin_messages = 0

    # 1) Stats por driver (con su empresa).
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT d.driver_id, d.name, d.phone_e164, d.notify_whatsapp, "
            "       d.opted_in_at, d.empresa_id, e.nombre AS empresa_nombre, "
            "       SUM(CASE WHEN LOWER(v.status)='completed' THEN 1 ELSE 0 END) AS ok_, "
            "       SUM(CASE WHEN LOWER(v.status)='failed'    THEN 1 ELSE 0 END) AS fail_, "
            "       COUNT(*) AS total_ "
            "FROM fpoc.simpli_visits v "
            "JOIN fpoc.drivers d ON d.vehicle_id = v.patente_falsa "
            "LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id "
            "WHERE v.planned_date = ? AND d.active = 1 "
            "GROUP BY d.driver_id, d.name, d.phone_e164, d.notify_whatsapp, "
            "         d.opted_in_at, d.empresa_id, e.nombre",
            fecha,
        )
        driver_rows = list(cur.fetchall())

    # 1a) Enviar resumen al driver.
    empresa_agg: dict[int, dict] = {}  # empresa_id → {nombre, ok, fail, total, drivers: []}
    for row in driver_rows:
        driver_id = str(row[0]) if row[0] else "?"
        driver_name = str(row[1] or "—")
        phone = str(row[2] or "").strip()
        notify = bool(row[3]) if row[3] is not None else False
        optin = row[4]
        emp_id = int(row[5]) if row[5] is not None else None
        emp_nombre = str(row[6] or "—")
        ok = int(row[7] or 0)
        fail = int(row[8] or 0)
        total = int(row[9] or 0)
        pct = int(round(100 * ok / max(1, total)))

        # Acumular para summary por empresa
        if emp_id is not None:
            agg = empresa_agg.setdefault(emp_id, {
                "nombre": emp_nombre, "ok": 0, "fail": 0, "total": 0, "drivers": []
            })
            agg["ok"] += ok
            agg["fail"] += fail
            agg["total"] += total
            agg["drivers"].append({"name": driver_name, "ok": ok, "fail": fail, "total": total})

        if not phone.startswith("+") or not notify or optin is None:
            continue
        body_driver = (
            f"🌙 *Día cerrado* — {fecha}\n"
            f"Tu resultado: *{ok}/{total} OK* ({pct}%)"
            + (f" · {fail} fallidas" if fail > 0 else "")
            + "\n¡Buen trabajo! Te avisamos cuando empiece el próximo turno."
        )
        try:
            send_whatsapp(
                body=body_driver,
                targets=[(None, phone)],
                subject=f"Día cerrado · {fecha}",
                tracking_id=None,
                triggered_by=triggered_by,
            )
            drivers_notified += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[day-close] driver {driver_id} fallo: {e}")

    # 2) Resumen por empresa → manager(es).
    for emp_id, agg in empresa_agg.items():
        ok = agg["ok"]; fail = agg["fail"]; total = agg["total"]
        pct = int(round(100 * ok / max(1, total)))
        body_mgr = (
            f"🌙 *Cierre {fecha}* · {agg['nombre']}\n"
            f"Total flota: *{ok}/{total} OK* ({pct}%)"
            + (f" · {fail} fallidas" if fail > 0 else "")
            + "\n"
            + "\n".join(
                f" • {d['name']}: {d['ok']}/{d['total']}" for d in agg["drivers"][:10]
            )
        )
        mgr_n, _ = _notify_supervisors(
            emp_id, body_mgr,
            subject=f"Cierre {fecha} · {agg['nombre']}",
            tracking_id=None,
            triggered_by=triggered_by,
        )
        manager_messages += mgr_n

    # 3) Resumen global cross-empresa para admin Falabella.
    if empresa_agg:
        total_ok = sum(a["ok"] for a in empresa_agg.values())
        total_total = sum(a["total"] for a in empresa_agg.values())
        total_fail = sum(a["fail"] for a in empresa_agg.values())
        pct_global = int(round(100 * total_ok / max(1, total_total)))
        body_admin = (
            f"🌙 *Cierre {fecha} · Vista Falabella*\n"
            f"Global: *{total_ok}/{total_total} OK* ({pct_global}%) · "
            f"{total_fail} fallidas\n"
            + "Por empresa:\n"
            + "\n".join(
                f" • {a['nombre']}: {a['ok']}/{a['total']}"
                for a in empresa_agg.values()
            )
        )
        # Admins reciben con empresa_id=None (cross-empresa).
        _, adm_n = _notify_supervisors(
            None, body_admin,
            subject=f"Cierre {fecha} · Global",
            tracking_id=None,
            triggered_by=triggered_by,
        )
        admin_messages += adm_n

    detail = (
        f"drivers={drivers_notified} mgrs={manager_messages} "
        f"admins={admin_messages} empresas={len(empresa_agg)}"
    )
    logger.info(f"[day-close-summary] {fecha} {detail}")

    return DayCloseSummaryResponse(
        fecha=fecha,
        drivers_notified=drivers_notified,
        manager_messages_sent=manager_messages,
        admin_messages_sent=admin_messages,
        detail=detail,
    )


def dispatch_visit_completed(
    tracking_id: str,
    *,
    triggered_by: str = "visit_completed_manual",
) -> VisitCompletedResponse:
    """Cuando una visita pasa a status='completed', avisa por WhatsApp a:

      1. El driver: "Entrega OK: {cliente}. Quedan N pendientes."
      2. Manager(es) de la empresa: contactos con recibe_alerts=1.
      3. Admin Falabella: contactos cross-empresa con recibe_alerts=1 (si los
         hay en empresa_contactos).

    Mensajes freeform — dependen de que cada destinatario tenga ventana 24h
    abierta. Si no, el envio falla pero no bloquea el flow operativo.

    Llamada desde:
      - POST /api/admin/pilot/simulate-event (event='complete')
      - Futuro: bot driver cuando marca entrega OK desde el menu FSM.
    """
    from routers.notifications import send_whatsapp

    tid = tracking_id.strip()
    today_iso = date.today().isoformat()

    # 1) Visita + datos del driver/empresa.
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

    # 2) Driver activo + empresa.
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

    # 3) Counters del día.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT "
            "  SUM(CASE WHEN LOWER(status)='completed' THEN 1 ELSE 0 END) AS done, "
            "  COUNT(*) AS total "
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
    logger.info(
        f"[visit-completed] filtro driver: phone={driver_phone!r} starts_plus={driver_phone.startswith('+')} "
        f"notify={driver_notify} optin_not_none={driver_optin is not None}"
    )
    if (driver_phone.startswith("+") and driver_notify and driver_optin is not None):
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
                f"[visit-completed] send_whatsapp result: status={first.status if first else 'no-results'} "
                f"sid={first.twilio_sid if first else None} err={first.error if first else None}"
            )
            if first and first.status in ("sent", "queued", "delivered", "dry_run"):
                driver_notified = True
            else:
                logger.warning(f"[visit-completed] driver {driver_id} no enviado: {first.error if first else 'no-result'}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[visit-completed] driver {driver_id} excepcion: {e}")

    # 4-5) Broadcast a supervisors (managers de la empresa + admins Falabella).
    body_supervisors = (
        f"✅ *{driver_name}* entregó \"{cliente_label}\" a las {hora_str}.\n"
        f"Empresa: *{empresa_nombre}* — Progreso: {completed_count}/{total_count} OK."
    )
    manager_notified, admin_notified = _notify_supervisors(
        empresa_id, body_supervisors,
        subject=f"Entrega OK · {empresa_nombre}",
        tracking_id=tid,
        triggered_by=triggered_by,
    )

    detail = (
        f"driver_notified={driver_notified} mgrs={manager_notified} "
        f"admins={admin_notified} ({completed_count}/{total_count})"
    )
    logger.info(f"[visit-completed] TID={tid} {detail}")

    return VisitCompletedResponse(
        tracking_id=tid,
        driver_notified=driver_notified,
        manager_notified_count=manager_notified,
        admin_notified_count=admin_notified,
        completed_count=completed_count,
        total_count=total_count,
        detail=detail,
    )

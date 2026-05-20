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

import os
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
# cliente, comentario). Aprobado por Meta. Mismo fallback hardcoded que el
# resto del codebase (state.py, comments.py, vip_deadline_cron.py).
_DEFAULT_ALERTA_MOTIVO_SID = "HX6821f9cad06ce1980bee5ad410006e43"


def _alerta_motivo_sid() -> str:
    return os.environ.get(
        "TWILIO_CONTENT_SID_ALERTA_MOTIVO", _DEFAULT_ALERTA_MOTIVO_SID
    )


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
                "ORDER BY opted_in_at DESC",
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
                    "ORDER BY \"order\" ASC, id ASC",
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

@router.post("/notify-eta-breach", response_model=NotifyEtaBreachResponse)
def notify_eta_breach(
    req: NotifyEtaBreachRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> NotifyEtaBreachResponse:
    """Manual trigger para disparar alerta de atraso ETA en una visita
    específica (uso QA / demo). Persiste el `tracking_id` en la sesión
    WhatsApp del driver para que el LLM agent pueda enlazar la respuesta
    libre del driver con el `report_motivo` correspondiente.
    """
    from routers.comments import _sanitize_template_var
    from routers.notifications import send_whatsapp

    tid = req.tracking_id.strip()

    # 1) SELECT visita.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT id, title, status, current_eta_cl, patente_falsa, "
            "       address, driver_name, comuna "
            "FROM fpoc.simpli_visits WHERE CAST(id AS TEXT) = ?",
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

    # 2) Resolver driver activo para esa patente.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT driver_id, name, phone_e164, notify_whatsapp, opted_in_at "
            "FROM fpoc.drivers "
            "WHERE vehicle_id = ? AND active = 1 "
            "ORDER BY opted_in_at DESC",
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
            triggered_by="eta_breach_manual",
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

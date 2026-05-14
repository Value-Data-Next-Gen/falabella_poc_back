"""CR-013 — Escalamiento a supervisor vía WhatsApp.

POST /api/whatsapp/escalate-supervisor
    Body: { tracking_id }

Flujo:
  1. Resolver visita (empresa_falsa, driver_name, comuna, address, etc.) en
     fpoc.simpli_visits.
  2. Resolver supervisor_phone_e164 de fpoc.empresas_transporte (col agregada en
     migración 023, CR-012). Si NULL → 409 supervisor_phone_not_configured.
  3. Anti-spam cooldown por (empresa_id, target='supervisor') usando el mismo
     `STATE._autonotify_last_sent` que core/state.py. Key dedicada para que la
     escalación NO comparta cooldown con notificaciones cliente.
     Ventana = AUTO_NOTIFY_COOLDOWN_SEC (default 300s). En cooldown → 429.
  4. Construir el template SERVER-SIDE (no se acepta del cliente — auditable).
  5. Enviar via Twilio reusando `routers.notifications.send_whatsapp`. Si no
     hay credenciales (`.env` vacío) cae automáticamente a dry-run y los
     resultados quedan logueados con status='dry_run' en notifications_log.
  6. Insertar en fpoc.alert_dispatch_log con type='driver_sin_respuesta'
     (CHECK constraint de la tabla acepta solo 3 values; usamos el que más
     se aproxima a una escalación por driver sin respuesta — el detalle real
     queda en payload_json.alert_type='escalation_supervisor').
  7. Devolver {dispatch_id, sent_at, dry_run}.

NO usa el wrapper genérico de cooldown en `STATE._auto_notify_alerts` porque
ese es solo para auto-notify de alertas anticipadas. Acá la acción es
explícita del operador y el cooldown solo busca evitar dobles clics + retry.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import backend as db_backend, get_conn
from core.state import STATE


router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp-escalation"])


# Mantener consistente con el CHECK constraint de fpoc.alert_dispatch_log
# (CR-012 / migración 023). Esa tabla solo acepta 3 valores en `type`:
# 'retraso_vip' | 'driver_sin_respuesta' | 'motivo_patron'. La escalación al
# supervisor encaja semánticamente con driver_sin_respuesta; el matiz fino
# (alert_type='escalation_supervisor') queda registrado en payload_json para
# que el reporting pueda separar ambos.
_DISPATCH_TYPE = "driver_sin_respuesta"
_DISPATCH_CHANNEL = "whatsapp"
_DISPATCH_TARGET = "supervisor"


# ---------- Schemas ----------
class EscalateSupervisorIn(BaseModel):
    tracking_id: str = Field(..., max_length=64, min_length=1)


class EscalateSupervisorOut(BaseModel):
    dispatch_id: int
    sent_at: datetime
    dry_run: bool


# ---------- Helpers ----------
def _resolve_visit(tracking_id: str) -> Optional[dict]:
    """Devuelve dict con datos mínimos de la visita o None si no existe.

    fpoc.simpli_visits.id es BIGINT en SQL Server: si llega un tracking_id no
    numérico (typo del cliente, fixture inválida), pyodbc tira `ConversionError`
    en el bind del parámetro. Lo atrapamos y tratamos como "no existe" para
    devolver 404 en vez de 500.
    """
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT id, empresa_falsa, ruta_id, driver_name, comuna, address,
                       title, current_eta_cl, sla_hour_checkout_eta
                FROM fpoc.simpli_visits
                WHERE id = ?
                """,
                tracking_id,
            )
            r = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.info(f"[escalation] _resolve_visit({tracking_id!r}) lookup falló: {e}")
        return None
    if r is None:
        return None
    return {
        "tracking_id": str(r.id),
        "empresa_id": int(r.empresa_falsa) if r.empresa_falsa is not None else None,
        "ruta_id": str(r.ruta_id) if getattr(r, "ruta_id", None) else None,
        "driver_name": str(r.driver_name) if r.driver_name else "",
        "comuna": str(r.comuna) if getattr(r, "comuna", None) else "",
        "address": str(r.address) if r.address else "",
        "title": str(r.title) if r.title else "",
        "current_eta_cl": str(r.current_eta_cl) if r.current_eta_cl else "",
        "sla_hour": float(r.sla_hour_checkout_eta) if r.sla_hour_checkout_eta is not None else 0.0,
    }


def _resolve_supervisor(empresa_id: int) -> tuple[Optional[str], Optional[str]]:
    """Devuelve (supervisor_phone_e164, nombre_empresa) o (None, nombre)."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT supervisor_phone_e164, nombre "
            "FROM fpoc.empresas_transporte WHERE empresa_id = ?",
            empresa_id,
        )
        r = cur.fetchone()
        if r is None:
            return None, None
        phone = getattr(r, "supervisor_phone_e164", None)
        nombre = getattr(r, "nombre", None)
        return (str(phone) if phone else None), (str(nombre) if nombre else None)


def _check_cooldown(empresa_id: int) -> Optional[int]:
    """Si está en cooldown devuelve los segundos restantes; sino None."""
    cooldown_sec = int(os.environ.get("AUTO_NOTIFY_COOLDOWN_SEC", "300"))
    if cooldown_sec <= 0:
        return None
    # Key dedicada distinta del cooldown por teléfono que usa auto-notify,
    # así el escalamiento manual no se ve bloqueado por notificaciones cliente
    # (y viceversa).
    key = f"escalation_supervisor:{empresa_id}"
    last = STATE._autonotify_last_sent.get(key)
    if last is None:
        return None
    delta = (datetime.utcnow() - last).total_seconds()
    if delta < cooldown_sec:
        return int(cooldown_sec - delta) + 1
    return None


def _mark_cooldown(empresa_id: int) -> None:
    key = f"escalation_supervisor:{empresa_id}"
    STATE._autonotify_last_sent[key] = datetime.utcnow()


def _build_template(visit: dict, empresa_nombre: Optional[str]) -> str:
    """Cuerpo WhatsApp construido server-side. NO se acepta del cliente."""
    title = visit.get("title") or "—"
    driver = visit.get("driver_name") or "—"
    comuna = visit.get("comuna") or "—"
    address = visit.get("address") or "—"
    eta = visit.get("current_eta_cl") or "—"
    tracking = visit.get("tracking_id")
    empresa = empresa_nombre or "—"
    return (
        f"[Falabella ValueData] ESCALAMIENTO Supervisor\n"
        f"Empresa: {empresa}\n"
        f"Tracking: {tracking}\n"
        f"Cliente: {title}\n"
        f"Driver: {driver}\n"
        f"Comuna: {comuna}\n"
        f"Direccion: {address}\n"
        f"ETA actual: {eta}\n"
        f"Accion: revisar driver / contactar cliente."
    )


def _insert_dispatch(
    *,
    tracking_id: str,
    empresa_id: int,
    ruta_id: Optional[str],
    payload: dict,
    user_id: int,
) -> tuple[int, datetime]:
    """INSERT en fpoc.alert_dispatch_log. Devuelve (dispatch_id, sent_at)."""
    payload_json = json.dumps(payload, ensure_ascii=False)
    with get_conn() as cn:
        cur = cn.cursor()
        if db_backend() == "sqlserver":
            cur.execute(
                """
                INSERT INTO fpoc.alert_dispatch_log
                  (tracking_id, type, channel, target, ruta_id, empresa_id,
                   payload_json, created_by_user_id)
                OUTPUT INSERTED.alert_id, INSERTED.sent_at
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tracking_id, _DISPATCH_TYPE, _DISPATCH_CHANNEL, _DISPATCH_TARGET,
                ruta_id, empresa_id, payload_json, user_id,
            )
            row = cur.fetchone()
            if row is None:
                cn.rollback()
                raise HTTPException(500, "INSERT no devolvió fila")
            dispatch_id = int(row[0])
            sent_at_raw = row[1]
        else:
            cur.execute(
                """
                INSERT INTO fpoc.alert_dispatch_log
                  (tracking_id, type, channel, target, ruta_id, empresa_id,
                   payload_json, created_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tracking_id, _DISPATCH_TYPE, _DISPATCH_CHANNEL, _DISPATCH_TARGET,
                ruta_id, empresa_id, payload_json, user_id,
            )
            cur.execute("SELECT last_insert_rowid()")
            row_id = cur.fetchone()
            dispatch_id = int(row_id[0]) if row_id and row_id[0] is not None else 0
            cur.execute(
                "SELECT sent_at FROM fpoc.alert_dispatch_log WHERE alert_id = ?",
                dispatch_id,
            )
            row_ts = cur.fetchone()
            sent_at_raw = row_ts[0] if row_ts else None
        cn.commit()

    if isinstance(sent_at_raw, datetime):
        sent_at = sent_at_raw
    elif isinstance(sent_at_raw, str):
        try:
            sent_at = datetime.fromisoformat(sent_at_raw.replace(" ", "T"))
        except ValueError:
            sent_at = datetime.now(timezone.utc)
    else:
        sent_at = datetime.now(timezone.utc)
    return dispatch_id, sent_at


# ---------- Endpoint ----------
@router.post("/escalate-supervisor", response_model=EscalateSupervisorOut)
def escalate_supervisor(
    req: EscalateSupervisorIn,
    response: Response,
    user: CurrentUser = Depends(current_user),
) -> EscalateSupervisorOut:
    # 1) Resolver visita
    visit = _resolve_visit(req.tracking_id)
    if visit is None:
        raise HTTPException(404, f"tracking_id {req.tracking_id} no encontrado")
    empresa_id = visit["empresa_id"]
    if empresa_id is None:
        raise HTTPException(409, {
            "error": "visit_without_empresa",
            "tracking_id": req.tracking_id,
        })

    # Scope: transport_manager solo puede escalar a su empresa.
    if not user.is_falabella and user.empresa_id != empresa_id:
        raise HTTPException(403, "No autorizado a escalar visitas de otra empresa")

    # 2) Supervisor phone
    sup_phone, empresa_nombre = _resolve_supervisor(empresa_id)
    if not sup_phone:
        raise HTTPException(409, {
            "error": "supervisor_phone_not_configured",
            "empresa_id": empresa_id,
        })

    # 3) Cooldown
    remaining = _check_cooldown(empresa_id)
    if remaining is not None:
        response.headers["Retry-After"] = str(remaining)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {
                "error": "cooldown_active",
                "empresa_id": empresa_id,
                "retry_after_sec": remaining,
            },
        )

    # 4) Build template server-side
    body = _build_template(visit, empresa_nombre)

    # 5) Enviar Twilio (cae a dry_run si no hay creds)
    # Import perezoso para no crear ciclo con notifications router.
    from routers.notifications import send_whatsapp

    # `triggered_by` queda limitado a 20 chars en fpoc.notifications_log
    # (matchear con los demás values: 'manual', 'auto_threshold', 'vip', etc.).
    # El detalle fino 'escalation_supervisor' va en payload_json.alert_type.
    wa_resp = send_whatsapp(
        body=body,
        targets=[(None, sup_phone)],
        subject=f"Escalamiento {visit['title']}",
        tracking_id=req.tracking_id,
        triggered_by="escalation",
    )
    dry_run = bool(wa_resp.dry_run)
    twilio_sid = wa_resp.results[0].twilio_sid if wa_resp.results else None
    send_status = wa_resp.results[0].status if wa_resp.results else "unknown"

    # 6) Mark cooldown ANTES del INSERT (la próxima request entrará en 429 aunque
    # el INSERT termine fallando; preferible a perdón-pide-perdón de dobles
    # envíos a Twilio).
    _mark_cooldown(empresa_id)

    # 7) Insert alert_dispatch_log
    payload = {
        "alert_type": "escalation_supervisor",   # fine-grained type
        "to": sup_phone,
        "body": body,
        "dry_run": dry_run,
        "twilio_sid": twilio_sid,
        "send_status": send_status,
        "triggered_by_user_email": user.email,
    }
    dispatch_id, sent_at = _insert_dispatch(
        tracking_id=req.tracking_id,
        empresa_id=empresa_id,
        ruta_id=visit.get("ruta_id"),
        payload=payload,
        user_id=user.user_id,
    )

    logger.info(
        f"[escalation] dispatch_id={dispatch_id} empresa={empresa_id} "
        f"tracking={req.tracking_id} dry_run={dry_run} by={user.email}"
    )

    return EscalateSupervisorOut(
        dispatch_id=dispatch_id,
        sent_at=sent_at,
        dry_run=dry_run,
    )

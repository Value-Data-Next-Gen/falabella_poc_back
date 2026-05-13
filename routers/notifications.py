"""Notificaciones vía Twilio WhatsApp + log en fpoc.notifications_log.

Soporta dos modos de envío:
  A) Freeform body      -> solo dentro de la ventana de 24h (72h sandbox) con el usuario.
  B) Content Template   -> content_sid + content_variables, válido siempre.

Config env:
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM       ej: 'whatsapp:+14155238886'
    TWILIO_CONTENT_SID         (opcional) Template por defecto para auto-notify
                                ej: 'HXb5b62575e6e4ff6129ad7c8efe1f983e'
    NOTIFICATIONS_DRY_RUN      'true' fuerza dry-run aunque haya creds
    NOTIFICATIONS_ENABLED      'false' desactiva todo

Endpoints (/api/notifications):
    POST /whatsapp   -> envía a user_ids o to_numbers. Acepta body O content_sid+variables.
    GET  /log        -> últimas N notificaciones (scope por empresa si no es falabella)
    POST /test       -> envía un mensaje de prueba al user actual
    GET  /config     -> estado del servicio
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field, model_validator

from core.auth import CurrentUser, current_user, require_admin
from core.db import backend as db_backend, get_conn

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# ---------- Cliente Twilio lazy ----------
@dataclass
class TwilioConfig:
    account_sid: str
    auth_token: str           # usado si no hay API Key
    api_key_sid: str          # SKxxx (opcional, preferido)
    api_key_secret: str       # secret de la API Key
    from_number: str
    default_content_sid: str  # template por defecto para auto-notify
    enabled: bool
    dry_run: bool

    @property
    def has_creds(self) -> bool:
        return bool(self.account_sid) and (
            bool(self.auth_token)
            or (bool(self.api_key_sid) and bool(self.api_key_secret))
        )

    @classmethod
    def from_env(cls) -> "TwilioConfig":
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        key_sid = os.environ.get("TWILIO_API_KEY_SID", "")
        key_secret = os.environ.get("TWILIO_API_KEY_SECRET", "")
        from_num = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        if not from_num.startswith("whatsapp:"):
            from_num = f"whatsapp:{from_num}"
        content_sid = os.environ.get("TWILIO_CONTENT_SID", "")
        enabled = os.environ.get("NOTIFICATIONS_ENABLED", "true").lower() != "false"
        cfg = cls(
            sid, token, key_sid, key_secret, from_num, content_sid,
            enabled, False,
        )
        cfg.dry_run = (
            os.environ.get("NOTIFICATIONS_DRY_RUN", "").lower() == "true"
            or not cfg.has_creds
        )
        return cfg


def _twilio_client():
    """Import perezoso — el paquete twilio puede no estar en dev local.
    Usa API Key si está configurada (preferido); sino Auth Token."""
    from twilio.rest import Client
    cfg = TwilioConfig.from_env()
    if cfg.api_key_sid and cfg.api_key_secret:
        # Client(username=KEY_SID, password=KEY_SECRET, account_sid=ACCOUNT_SID)
        return Client(cfg.api_key_sid, cfg.api_key_secret, cfg.account_sid), cfg
    return Client(cfg.account_sid, cfg.auth_token), cfg


# ---------- Schemas ----------
class WhatsAppRequest(BaseModel):
    # Modo A: freeform
    body: Optional[str] = Field(default=None, max_length=1500)
    # Modo B: content template
    content_sid: Optional[str] = Field(default=None, max_length=100)
    content_variables: Optional[dict[str, str]] = None

    to_user_ids: list[int] | None = None
    to_numbers: list[str] | None = None  # E.164 (+569...)
    tracking_id: Optional[str] = None
    subject: Optional[str] = None
    triggered_by: str = "manual"  # 'manual' | 'auto_threshold' | 'vip'

    @model_validator(mode="after")
    def _at_least_one_payload(self):
        if not self.body and not self.content_sid:
            raise ValueError("Debe proveer 'body' o 'content_sid'")
        return self


class NotificationResult(BaseModel):
    to_number: str
    status: str           # 'sent' | 'dry_run' | 'error'
    twilio_sid: Optional[str] = None
    error: Optional[str] = None
    user_id: Optional[int] = None


class WhatsAppResponse(BaseModel):
    dry_run: bool
    sent: int
    failed: int
    results: list[NotificationResult]


class TrackingNotifSummary(BaseModel):
    tracking_id: str
    count: int
    sent_count: int
    last_status: str
    last_to: str
    last_body: str
    last_triggered_by: str
    last_created_at: str
    last_twilio_sid: Optional[str] = None
    last_content_sid: Optional[str] = None
    last_content_variables: Optional[dict] = None


class NotificationLogRow(BaseModel):
    notification_id: int
    user_id: Optional[int] = None
    to_number: str
    channel: str
    subject: Optional[str] = None
    body: str
    tracking_id: Optional[str] = None
    twilio_sid: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    triggered_by: str
    created_at: str
    direction: Optional[str] = None       # 'inbound' | 'outbound'
    profile_name: Optional[str] = None    # nombre WA del remitente (en inbound)


# ---------- Helpers ----------
def _log_notification(
    cn,
    *,
    user_id: Optional[int],
    to_number: str,
    body: str,
    status: str,
    subject: Optional[str] = None,
    tracking_id: Optional[str] = None,
    twilio_sid: Optional[str] = None,
    error: Optional[str] = None,
    triggered_by: str = "manual",
    content_sid: Optional[str] = None,
    content_variables: Optional[dict] = None,
    region: Optional[str] = None,
) -> int:
    cur = cn.cursor()
    cv_json = json.dumps(content_variables) if content_variables else None
    if db_backend() == "sqlserver":
        # OUTPUT INSERTED.notification_id es 100% confiable con pyodbc;
        # SCOPE_IDENTITY() en cur.execute separado a veces devuelve NULL.
        cur.execute(
            """
            INSERT INTO fpoc.notifications_log
              (user_id, to_number, channel, subject, body, tracking_id,
               twilio_sid, status, error_msg, triggered_by,
               content_sid, content_variables, region)
            OUTPUT INSERTED.notification_id
            VALUES (?, ?, 'whatsapp', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            user_id, to_number, subject, body, tracking_id,
            twilio_sid, status, error, triggered_by,
            content_sid, cv_json, region,
        )
        row = cur.fetchone()
        new_id = int(row[0]) if row and row[0] is not None else 0
    else:
        cur.execute(
            """
            INSERT INTO fpoc.notifications_log
              (user_id, to_number, channel, subject, body, tracking_id,
               twilio_sid, status, error_msg, triggered_by,
               content_sid, content_variables, region)
            VALUES (?, ?, 'whatsapp', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            user_id, to_number, subject, body, tracking_id,
            twilio_sid, status, error, triggered_by,
            content_sid, cv_json, region,
        )
        cur.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        new_id = int(row[0]) if row and row[0] is not None else 0
    cn.commit()
    return new_id


def _resolve_recipients(cn, user_ids: list[int]) -> list[tuple[int, str]]:
    """Devuelve [(user_id, phone_e164)] filtrando los que no tienen phone o notify_whatsapp=0."""
    if not user_ids:
        return []
    marks = ",".join(["?"] * len(user_ids))
    cur = cn.cursor()
    cur.execute(
        f"""
        SELECT user_id, phone_e164
        FROM fpoc.users
        WHERE user_id IN ({marks})
          AND activo = 1
          AND notify_whatsapp = 1
          AND phone_e164 IS NOT NULL
          AND LEN(phone_e164) > 0
        """,
        *user_ids,
    )
    return [(int(r.user_id), r.phone_e164) for r in cur.fetchall()]


def _send_one(
    client, cfg: TwilioConfig, to_number: str,
    body: Optional[str], content_sid: Optional[str], content_variables: Optional[dict],
) -> tuple[str, Optional[str], Optional[str]]:
    """Devuelve (status, twilio_sid, error_msg)."""
    to = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
    try:
        kwargs: dict[str, Any] = {"from_": cfg.from_number, "to": to}
        if content_sid:
            kwargs["content_sid"] = content_sid
            if content_variables:
                kwargs["content_variables"] = json.dumps(content_variables)
        else:
            kwargs["body"] = body
        msg = client.messages.create(**kwargs)
        return ("sent", msg.sid, None)
    except Exception as e:  # noqa: BLE001
        return ("error", None, str(e)[:500])


def send_whatsapp(
    *,
    body: Optional[str] = None,
    content_sid: Optional[str] = None,
    content_variables: Optional[dict] = None,
    targets: list[tuple[Optional[int], str]],
    subject: Optional[str] = None,
    tracking_id: Optional[str] = None,
    triggered_by: str = "manual",
) -> WhatsAppResponse:
    """Core: envía a N destinatarios. Requiere body O content_sid."""
    if not body and not content_sid:
        raise ValueError("send_whatsapp: body o content_sid requerido")

    cfg = TwilioConfig.from_env()
    results: list[NotificationResult] = []

    if not cfg.enabled:
        logger.info("[notifications] NOTIFICATIONS_ENABLED=false, skipping")
        return WhatsAppResponse(dry_run=True, sent=0, failed=0, results=[])

    client = None
    if not cfg.dry_run:
        try:
            client, _ = _twilio_client()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[notifications] Twilio init falló, cayendo a dry-run: {e}")
            cfg.dry_run = True

    # Snapshot para log: si es template y no hay body, guardamos una representación legible.
    body_for_log = body or f"[template:{content_sid}] {json.dumps(content_variables or {}, ensure_ascii=False)}"

    with get_conn() as cn:
        for user_id, to_number in targets:
            if cfg.dry_run:
                status, sid, err = "dry_run", None, None
                logger.info(f"[notifications][dry-run] {to_number}: {body_for_log[:120]}")
            else:
                status, sid, err = _send_one(
                    client, cfg, to_number,
                    body=body, content_sid=content_sid, content_variables=content_variables,
                )

            _log_notification(
                cn,
                user_id=user_id, to_number=to_number, body=body_for_log,
                subject=subject, tracking_id=tracking_id,
                status=status, twilio_sid=sid, error=err, triggered_by=triggered_by,
                content_sid=content_sid, content_variables=content_variables,
            )
            results.append(NotificationResult(
                to_number=to_number, status=status,
                twilio_sid=sid, error=err, user_id=user_id,
            ))

    sent = sum(1 for r in results if r.status == "sent")
    failed = sum(1 for r in results if r.status == "error")
    return WhatsAppResponse(dry_run=cfg.dry_run, sent=sent, failed=failed, results=results)


# ---------- Endpoints ----------
@router.post("/whatsapp", response_model=WhatsAppResponse)
def send(req: WhatsAppRequest, user: CurrentUser = Depends(current_user)) -> WhatsAppResponse:
    targets: list[tuple[Optional[int], str]] = []

    # Por user_id (resuelve phone desde DB y valida scope)
    if req.to_user_ids:
        with get_conn() as cn:
            recipients = _resolve_recipients(cn, req.to_user_ids)
            # Scope: transport_manager solo puede notificar users de su empresa
            if not user.is_falabella:
                cur = cn.cursor()
                marks = ",".join(["?"] * len(req.to_user_ids))
                cur.execute(
                    f"SELECT user_id FROM fpoc.users WHERE user_id IN ({marks}) AND empresa_id = ?",
                    *req.to_user_ids, user.empresa_id,
                )
                allowed = {int(r[0]) for r in cur.fetchall()}
                recipients = [(uid, p) for uid, p in recipients if uid in allowed]
        targets.extend(recipients)

    # Números directos (solo admin/ops)
    if req.to_numbers:
        if not user.is_falabella:
            raise HTTPException(403, "Solo falabella_* puede enviar a números libres")
        for n in req.to_numbers:
            targets.append((None, n))

    if not targets:
        raise HTTPException(400, "Sin destinatarios válidos (usuarios sin phone o notify_whatsapp=0)")

    return send_whatsapp(
        body=req.body,
        content_sid=req.content_sid,
        content_variables=req.content_variables,
        targets=targets,
        subject=req.subject,
        tracking_id=req.tracking_id,
        triggered_by=req.triggered_by,
    )


@router.post("/test", response_model=WhatsAppResponse)
def test_self(user: CurrentUser = Depends(current_user)) -> WhatsAppResponse:
    """Envía un mensaje de prueba al usuario actual (si tiene phone + notify_whatsapp)."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT phone_e164, notify_whatsapp FROM fpoc.users WHERE user_id = ?",
            user.user_id,
        )
        r = cur.fetchone()
    if not r or not r.phone_e164:
        raise HTTPException(400, "Tu usuario no tiene phone_e164 configurado")
    if not r.notify_whatsapp:
        raise HTTPException(400, "Tu usuario tiene notify_whatsapp=0")
    cfg = TwilioConfig.from_env()
    if cfg.default_content_sid:
        # Modo template: mandamos variables básicas de demo
        return send_whatsapp(
            content_sid=cfg.default_content_sid,
            content_variables={"1": "prueba", "2": user.display_name[:20]},
            targets=[(user.user_id, r.phone_e164)],
            subject="Test",
            triggered_by="manual",
        )
    body = f"[ValueData] Prueba de notificaciones — {user.display_name}"
    return send_whatsapp(
        body=body,
        targets=[(user.user_id, r.phone_e164)],
        subject="Test",
        triggered_by="manual",
    )


@router.get("/log", response_model=list[NotificationLogRow])
def get_log(
    limit: int = Query(default=50, ge=1, le=500),
    triggered_by: Optional[str] = Query(default=None, description="Filtra por origen: manual / auto_threshold / comment_alert / vip_deadline_warning"),
    status: Optional[str] = Query(default=None, description="Filtra por status: sent / dry_run / error"),
    direction: Optional[str] = Query(default=None, description="'inbound' (recibidos del driver) | 'outbound' (enviados desde el sistema)"),
    user: CurrentUser = Depends(current_user),
) -> list[NotificationLogRow]:
    extra_where: list[str] = []
    extra_params: list = []
    if triggered_by:
        extra_where.append("triggered_by = ?")
        extra_params.append(triggered_by)
    if status:
        extra_where.append("status = ?")
        extra_params.append(status)
    if direction in ("inbound", "outbound"):
        extra_where.append("COALESCE(direction, 'outbound') = ?")
        extra_params.append(direction)

    with get_conn() as cn:
        cur = cn.cursor()
        if user.is_falabella:
            where_sql = ""
            if extra_where:
                where_sql = " WHERE " + " AND ".join(extra_where)
            cur.execute(
                f"""
                SELECT notification_id, user_id, to_number, channel, subject,
                       body, tracking_id, twilio_sid, status, error_msg, triggered_by,
                       created_at, COALESCE(direction, 'outbound') AS direction, profile_name
                FROM fpoc.notifications_log
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                *extra_params, limit,
            )
        else:
            # solo las dirigidas a users de su empresa (outbound) o de contactos de su empresa (inbound)
            extra_where_aliased = []
            for c in extra_where:
                col = c.split(' = ')[0]
                # COALESCE(direction, 'outbound') stays unaliased (column expr).
                if "COALESCE" in col:
                    extra_where_aliased.append(c)
                else:
                    extra_where_aliased.append(f"l.{col} = ?")
            where_sql = " AND " + " AND ".join(extra_where_aliased) if extra_where_aliased else ""
            cur.execute(
                f"""
                SELECT l.notification_id, l.user_id, l.to_number, l.channel, l.subject,
                       l.body, l.tracking_id, l.twilio_sid, l.status, l.error_msg, l.triggered_by,
                       l.created_at, COALESCE(l.direction, 'outbound') AS direction, l.profile_name
                FROM fpoc.notifications_log l
                LEFT JOIN fpoc.users u ON u.user_id = l.user_id
                LEFT JOIN fpoc.empresa_contactos c ON c.contact_id = l.contact_id
                WHERE (u.empresa_id = ? OR c.empresa_id = ?){where_sql}
                ORDER BY l.created_at DESC
                LIMIT ?
                """,
                user.empresa_id, user.empresa_id, *extra_params, limit,
            )
        rows = cur.fetchall()
    return [
        NotificationLogRow(
            notification_id=int(r.notification_id),
            user_id=int(r.user_id) if r.user_id is not None else None,
            to_number=r.to_number,
            channel=r.channel,
            subject=r.subject,
            body=r.body,
            tracking_id=r.tracking_id,
            twilio_sid=r.twilio_sid,
            status=r.status,
            error_msg=r.error_msg,
            triggered_by=r.triggered_by,
            created_at=r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at),
            direction=getattr(r, "direction", None),
            profile_name=getattr(r, "profile_name", None),
        )
        for r in rows
    ]


@router.get("/by-trackings", response_model=dict[str, TrackingNotifSummary])
def by_trackings(
    ids: str = Query(..., description="tracking_ids separados por coma"),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """Para cada tracking_id, devuelve resumen + última notificación.
    Solo incluye tracking_ids que tengan al menos un envío registrado."""
    id_list = [s.strip() for s in ids.split(",") if s.strip()]
    if not id_list:
        return {}
    # SQL Server: IN con 2100 params max. Batching.
    out: dict[str, dict] = {}
    with get_conn() as cn:
        cur = cn.cursor()
        for i in range(0, len(id_list), 500):
            chunk = id_list[i:i + 500]
            marks = ",".join(["?"] * len(chunk))
            # Window function: ROW_NUMBER para la última notificación por tracking_id
            if user.is_falabella:
                cur.execute(
                    f"""
                    WITH ranked AS (
                      SELECT l.*, ROW_NUMBER() OVER (
                        PARTITION BY l.tracking_id ORDER BY l.created_at DESC
                      ) AS rn
                      FROM fpoc.notifications_log l
                      WHERE l.tracking_id IN ({marks})
                    ),
                    counts AS (
                      SELECT tracking_id, COUNT(*) AS total,
                             SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent_count
                      FROM fpoc.notifications_log
                      WHERE tracking_id IN ({marks})
                      GROUP BY tracking_id
                    )
                    SELECT r.tracking_id, c.total, c.sent_count,
                           r.status, r.to_number, r.body, r.triggered_by, r.created_at,
                           r.twilio_sid, r.content_sid, r.content_variables
                    FROM ranked r
                    INNER JOIN counts c ON c.tracking_id = r.tracking_id
                    WHERE r.rn = 1
                    """,
                    *chunk, *chunk,
                )
            else:
                cur.execute(
                    f"""
                    WITH ranked AS (
                      SELECT l.*, ROW_NUMBER() OVER (
                        PARTITION BY l.tracking_id ORDER BY l.created_at DESC
                      ) AS rn
                      FROM fpoc.notifications_log l
                      INNER JOIN fpoc.users u ON u.user_id = l.user_id
                      WHERE l.tracking_id IN ({marks}) AND u.empresa_id = ?
                    ),
                    counts AS (
                      SELECT l.tracking_id, COUNT(*) AS total,
                             SUM(CASE WHEN l.status='sent' THEN 1 ELSE 0 END) AS sent_count
                      FROM fpoc.notifications_log l
                      INNER JOIN fpoc.users u ON u.user_id = l.user_id
                      WHERE l.tracking_id IN ({marks}) AND u.empresa_id = ?
                      GROUP BY l.tracking_id
                    )
                    SELECT r.tracking_id, c.total, c.sent_count,
                           r.status, r.to_number, r.body, r.triggered_by, r.created_at,
                           r.twilio_sid, r.content_sid, r.content_variables
                    FROM ranked r
                    INNER JOIN counts c ON c.tracking_id = r.tracking_id
                    WHERE r.rn = 1
                    """,
                    *chunk, user.empresa_id, *chunk, user.empresa_id,
                )
            for r in cur.fetchall():
                cv = None
                if r.content_variables:
                    try:
                        cv = json.loads(r.content_variables)
                    except Exception:  # noqa: BLE001
                        cv = None
                out[r.tracking_id] = {
                    "tracking_id": r.tracking_id,
                    "count": int(r.total),
                    "sent_count": int(r.sent_count or 0),
                    "last_status": r.status,
                    "last_to": r.to_number,
                    "last_body": r.body,
                    "last_triggered_by": r.triggered_by,
                    "last_created_at": r.created_at.isoformat(),
                    "last_twilio_sid": r.twilio_sid,
                    "last_content_sid": r.content_sid,
                    "last_content_variables": cv,
                }
    return out


@router.get("/config")
def get_config(user: CurrentUser = Depends(current_user)) -> dict:
    """Devuelve el estado del servicio sin exponer secretos."""
    cfg = TwilioConfig.from_env()
    return {
        "enabled": cfg.enabled,
        "dry_run": cfg.dry_run,
        "from_number": cfg.from_number if user.is_falabella else "hidden",
        "has_creds": cfg.has_creds,
        "auth_mode": "api_key" if (cfg.api_key_sid and cfg.api_key_secret) else ("auth_token" if cfg.auth_token else "none"),
        "default_content_sid": cfg.default_content_sid if user.is_falabella else None,
        "mode": "template" if cfg.default_content_sid else "freeform",
    }


@router.post("/toggle")
def toggle_notifications(
    enabled: Optional[bool] = None,
    dry_run: Optional[bool] = None,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Toggle runtime (admin only). Sobreescribe env vars NOTIFICATIONS_ENABLED
    y NOTIFICATIONS_DRY_RUN para esta instancia del proceso. Útil cuando no
    se quiere tocar Azure App Service settings + restart.

    Nota: el cambio se pierde al reiniciar el proceso. Para persistir, hay que
    actualizar las Application Settings.
    """
    if enabled is not None:
        os.environ["NOTIFICATIONS_ENABLED"] = "true" if enabled else "false"
    if dry_run is not None:
        os.environ["NOTIFICATIONS_DRY_RUN"] = "true" if dry_run else "false"
    cfg = TwilioConfig.from_env()
    logger.info(f"[notifications] runtime toggle by {user.email}: enabled={cfg.enabled} dry_run={cfg.dry_run}")
    return {
        "ok": True,
        "enabled": cfg.enabled,
        "dry_run": cfg.dry_run,
        "note": "Cambio aplicado al proceso actual. Para persistir entre reinicios, actualizá las Application Settings en Azure.",
    }

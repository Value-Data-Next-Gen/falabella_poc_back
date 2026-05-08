"""Webhook inbound de Twilio WhatsApp.

Recibe mensajes que envía un usuario al sandbox (+14155238886) o al sender
registrado en producción. Twilio entrega `application/x-www-form-urlencoded`
con campos: From, Body, MessageSid, ProfileName, WaId, AccountSid, NumMedia,
MediaUrl0...N, MediaContentType0...N.

Endpoints:
  POST /api/twilio/inbound       -> handler. Devuelve TwiML (XML) para responder.
  GET  /api/twilio/inbound/test  -> ping (debug, sin validar firma).

Configuración:
  TWILIO_AUTH_TOKEN                   token con el que Twilio firma la request
  TWILIO_INBOUND_VALIDATE_SIGNATURE   'false' desactiva validación (default true)
  TWILIO_INBOUND_DEFAULT_EMPRESA_ID   empresa donde caen contactos nuevos (auto-onboard)
  TWILIO_INBOUND_PUBLIC_URL           URL pública con la que Twilio firma (necesaria
                                       cuando estás detrás de ngrok, porque request.url
                                       puede llegar como http://127.0.0.1)

Comandos soportados (case-insensitive, espacios permitidos):
  status <tracking_id>        → estado actual de la visita
  reagendar <tid> <HH:MM>     → marca reagendamiento (registra comment)
  motivo <tid> <MOTIVO>: <c>  → registra comentario con motivo
  help                         → lista de comandos
  (cualquier otro texto)       → si el número no está registrado, auto-onboarding;
                                 si está, solo log.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from loguru import logger

from db import get_conn


router = APIRouter(prefix="/api/twilio", tags=["twilio-inbound"])


# =============================================================================
# Helpers
# =============================================================================
def _normalize_phone(raw: str) -> str:
    """Quita prefijo whatsapp: y normaliza a E.164 con +."""
    p = (raw or "").strip()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    if p and not p.startswith("+"):
        p = "+" + p
    return p


def _twiml(text: Optional[str] = None) -> Response:
    """Devuelve TwiML. Sin texto = ack vacío. Con texto = mensaje de respuesta."""
    if text:
        # XML escape mínimo
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{safe}</Message></Response>"
    else:
        body = "<?xml version='1.0' encoding='UTF-8'?><Response/>"
    return Response(content=body, media_type="application/xml")


def _validate_signature(
    auth_token: str,
    public_url: str,
    form: dict[str, str],
    signature: Optional[str],
) -> bool:
    """Validación HMAC-SHA1 según docs Twilio. Retorna True si válida o si
    validación está deshabilitada."""
    if os.environ.get("TWILIO_INBOUND_VALIDATE_SIGNATURE", "true").lower() == "false":
        return True
    if not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator
        return RequestValidator(auth_token).validate(public_url, form, signature)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] signature validation error: {e}")
        return False


# =============================================================================
# Lookup: ¿quién es este número?
# =============================================================================
def _identify_phone(phone_e164: str) -> dict:
    """Busca el número entre users / contactos / drivers. Devuelve dict con
    flags + ids encontrados."""
    out = {
        "user_id": None,
        "user_role": None,
        "user_empresa_id": None,
        "contact_id": None,
        "contact_empresa_id": None,
        "driver_id": None,
        "is_known": False,
    }
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT user_id, role, empresa_id FROM fpoc_users "
            "WHERE phone_e164 = ? AND activo = 1 LIMIT 1",
            (phone_e164,),
        )
        r = cur.fetchone()
        if r is not None:
            out["user_id"] = int(r[0])
            out["user_role"] = str(r[1])
            out["user_empresa_id"] = int(r[2]) if r[2] is not None else None
            out["is_known"] = True

        cur.execute(
            "SELECT contact_id, empresa_id FROM fpoc_empresa_contactos "
            "WHERE phone_e164 = ? AND active = 1 LIMIT 1",
            (phone_e164,),
        )
        r = cur.fetchone()
        if r is not None:
            out["contact_id"] = int(r[0])
            out["contact_empresa_id"] = int(r[1]) if r[1] is not None else None
            out["is_known"] = True

        cur.execute(
            "SELECT driver_id FROM fpoc_drivers "
            "WHERE phone_e164 = ? AND active = 1 LIMIT 1",
            (phone_e164,),
        )
        r = cur.fetchone()
        if r is not None:
            out["driver_id"] = str(r[0])
            out["is_known"] = True
    return out


def _auto_onboard(phone_e164: str, profile_name: Optional[str]) -> Optional[int]:
    """Inserta el número como contacto en la empresa default. Devuelve contact_id
    o None si no hay empresa default o falla."""
    default_empresa = os.environ.get("TWILIO_INBOUND_DEFAULT_EMPRESA_ID", "").strip()
    with get_conn() as cn:
        cur = cn.cursor()
        if default_empresa:
            try:
                empresa_id = int(default_empresa)
            except ValueError:
                logger.warning(f"[twilio-inbound] TWILIO_INBOUND_DEFAULT_EMPRESA_ID inválido: {default_empresa}")
                return None
        else:
            # Tomar la primera empresa activa
            cur.execute("SELECT empresa_id FROM fpoc_empresas_transporte WHERE activo = 1 ORDER BY empresa_id LIMIT 1")
            r = cur.fetchone()
            if r is None:
                return None
            empresa_id = int(r[0])

        nombre = (profile_name or "").strip() or f"Contacto WhatsApp {phone_e164}"
        try:
            cur.execute(
                """
                INSERT INTO fpoc_empresa_contactos
                  (empresa_id, nombre, rol, phone_e164, opted_in_at, active, notes)
                VALUES (?, ?, 'otro', ?, CURRENT_TIMESTAMP, 1, 'auto-onboard via WhatsApp inbound')
                """,
                (empresa_id, nombre, phone_e164),
            )
            cn.commit()
            cur.execute("SELECT last_insert_rowid()")
            return int(cur.fetchone()[0])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[twilio-inbound] auto-onboard insert falló: {e}")
            return None


# =============================================================================
# Logging
# =============================================================================
def _log_inbound(
    *,
    from_number: str,
    body: str,
    twilio_sid: Optional[str],
    profile_name: Optional[str],
    media_urls: Optional[str],
    user_id: Optional[int],
    contact_id: Optional[int],
    driver_id: Optional[str],
    tracking_id: Optional[str] = None,
    triggered_by: str = "inbound",
) -> int:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            INSERT INTO fpoc_notifications_log
              (user_id, contact_id, driver_id, to_number, channel, body,
               tracking_id, twilio_sid, status, triggered_by,
               profile_name, media_urls, direction)
            VALUES (?, ?, ?, ?, 'whatsapp', ?, ?, ?, 'received', ?, ?, ?, 'inbound')
            """,
            (user_id, contact_id, driver_id, from_number, body, tracking_id,
             twilio_sid, triggered_by, profile_name, media_urls),
        )
        cn.commit()
        cur.execute("SELECT last_insert_rowid()")
        return int(cur.fetchone()[0])


# =============================================================================
# Command parser
# =============================================================================
_RE_STATUS = re.compile(r"^\s*status\s+(\S+)\s*$", re.IGNORECASE)
_RE_REAGENDAR = re.compile(
    r"^\s*reagendar\s+(\S+)\s+(\d{1,2}:\d{2})\s*$", re.IGNORECASE
)
_RE_MOTIVO = re.compile(
    r"^\s*motivo\s+(\S+)\s+([A-ZÁÉÍÓÚÑ /]+):\s*(.+)$", re.IGNORECASE
)
_RE_HELP = re.compile(r"^\s*(help|ayuda|menu|comandos)\s*$", re.IGNORECASE)
_RE_INFO = re.compile(r"^\s*(info|que es esto|qué es esto|sobre)\s*$", re.IGNORECASE)
_RE_HUMAN = re.compile(
    r"^\s*(humano|operador|hablar con alguien|persona|atencion|atención)\s*$",
    re.IGNORECASE,
)
_RE_STOP = re.compile(
    r"^\s*(stop|baja|desuscribir|unsubscribe|salir|cancelar)\s*$", re.IGNORECASE
)
_RE_THANKS = re.compile(
    r"^\s*(gracias|ok|okey|recibido|listo|👍|👌|✅|copiado|dale)\s*$",
    re.IGNORECASE,
)
_RE_KPIS = re.compile(r"^\s*kpis?\s*(hoy)?\s*$", re.IGNORECASE)


def _cmd_status(tracking_id: str) -> str:
    from state import STATE
    if STATE.snapshot_df is None:
        return "Backend no listo."
    df = STATE.snapshot_df
    matching = df[df["tracking_id"] == tracking_id]
    if matching.empty:
        return f"No encuentro {tracking_id}."
    row = matching.iloc[0]
    return (
        f"Visita {tracking_id}\n"
        f"Cliente: {row['title']}\n"
        f"Vehículo: {row['vehicle_name']}\n"
        f"Estado: {row['status']}\n"
        f"ETA: {row['estimated_time_arrival']}\n"
        f"Window end: {row['window_end']}\n"
        f"Riesgo: {float(row['p_fallo'])*100:.0f}%"
    )


def _cmd_reagendar(tracking_id: str, hh_mm: str, identity: dict) -> str:
    """POC: registramos un comment 'CLIENTE RECHAZA' con texto 'reagendado a HH:MM'."""
    try:
        from comments import _persist_and_dispatch_comment
        actor = "WhatsApp inbound"
        if identity.get("user_id"):
            actor = f"user_id={identity['user_id']}"
        _persist_and_dispatch_comment(
            tracking_id=tracking_id,
            motivo="CLIENTE RECHAZA",
            comentario=f"Reagendado a {hh_mm} por {actor} via WhatsApp",
            user_id=identity.get("user_id"),
            user_display_name=actor,
        )
        return f"Reagendamiento registrado para {tracking_id} a las {hh_mm}."
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] reagendar falló: {e}")
        return f"No pude registrar el reagendamiento: {e}"


def _cmd_motivo(tracking_id: str, motivo: str, comentario: str, identity: dict) -> str:
    try:
        from comments import _persist_and_dispatch_comment, MOTIVOS_CATALOGO
        # match motivo case-insensitive
        m_norm = motivo.upper().strip()
        match = next((m for m in MOTIVOS_CATALOGO if m.upper() == m_norm), None)
        if match is None:
            return (
                f"Motivo '{motivo}' no reconocido. "
                f"Válidos: {', '.join(MOTIVOS_CATALOGO[:5])}..."
            )
        actor = identity.get("driver_id") or identity.get("user_id") or "WhatsApp inbound"
        _persist_and_dispatch_comment(
            tracking_id=tracking_id,
            motivo=match,
            comentario=comentario,
            user_id=identity.get("user_id"),
            user_display_name=str(actor),
        )
        return f"Comentario registrado en {tracking_id} con motivo {match}."
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] motivo falló: {e}")
        return f"No pude registrar el comentario: {e}"


def _cmd_help() -> str:
    return (
        "Comandos:\n"
        "• status <tracking_id>\n"
        "• kpis — resumen del día\n"
        "• reagendar <tracking_id> <HH:MM>\n"
        "• motivo <tracking_id> <MOTIVO>: <comentario>\n"
        "• humano — escalar a operador\n"
        "• stop — desuscribirse\n"
        "• help"
    )


def _cmd_info() -> str:
    return (
        "Falabella ValueData — torre de control logística.\n"
        "Recibís alertas anticipadas de visitas en riesgo. "
        "Escribe 'help' para comandos o 'stop' para desuscribirte."
    )


def _cmd_human(phone: str, identity: dict) -> str:
    """Marca el último log inbound como escalado para que un operador lo tome."""
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                UPDATE fpoc_notifications_log
                SET triggered_by = 'inbound_escalated'
                WHERE notification_id = (
                    SELECT MAX(notification_id) FROM fpoc_notifications_log
                    WHERE direction = 'inbound' AND to_number = ?
                )
                """,
                (phone,),
            )
            cn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] escalar falló: {e}")
    return (
        "Te escalé a un operador. En unos minutos te contactan por WhatsApp. "
        "Si es urgente, llamá al call center."
    )


def _cmd_unsubscribe(phone: str, identity: dict) -> str:
    """Apaga el opt-in del contacto/usuario asociado al número.
    Por compliance de WhatsApp: honramos opt-out inmediato."""
    rows_affected = 0
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc_empresa_contactos SET opted_in_at = NULL, active = 0 "
                "WHERE phone_e164 = ?",
                (phone,),
            )
            rows_affected += cur.rowcount or 0
            cur.execute(
                "UPDATE fpoc_users SET notify_whatsapp = 0 WHERE phone_e164 = ?",
                (phone,),
            )
            rows_affected += cur.rowcount or 0
            cur.execute(
                "UPDATE fpoc_drivers SET notify_whatsapp = 0, opted_in_at = NULL "
                "WHERE phone_e164 = ?",
                (phone,),
            )
            rows_affected += cur.rowcount or 0
            cn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] unsubscribe falló: {e}")
        return "Tuvimos un problema procesando tu baja. Reenviá 'stop' o contactá al operador."
    if rows_affected == 0:
        return "No encontré tu número en la lista. Si seguís recibiendo mensajes, contactá al operador."
    return "Listo, te dimos de baja. No vas a recibir más alertas. Para reactivar, mandá 'join <código>' al sandbox."


def _cmd_thanks() -> str:
    return "👍"


def _cmd_kpis() -> str:
    from state import STATE
    if STATE.snapshot_df is None:
        return "Backend no listo."
    df = STATE.snapshot_df
    pending = int((df["status"] == "pending").sum())
    alerts = int(df["alert_valuedata"].sum())
    completed = int((df["status"] == "completed").sum())
    return (
        f"KPIs hoy ({STATE.today.isoformat() if STATE.today else '?'}):\n"
        f"• Visitas: {len(df)}\n"
        f"• Pendientes: {pending}\n"
        f"• Completadas: {completed}\n"
        f"• Alertas anticipadas: {alerts}"
    )


def _dispatch(body: str, identity: dict, phone: str, profile_name: Optional[str] = None) -> Optional[str]:
    """Devuelve respuesta TwiML o None si no hay match.

    Prioridades:
      1) Compliance: opt-out (stop) — siempre gana.
      2) Comandos sueltos (power users): status/motivo/kpis/help/etc.
      3) Agente conversacional FSM (whatsapp_agent.handle).
    """
    if not body:
        return None
    # 1) Compliance primero: opt-out manda y termina cualquier sesión activa.
    if _RE_STOP.match(body):
        try:
            from whatsapp_agent import Session as _WaSession
            _WaSession.delete(phone)
        except Exception:  # noqa: BLE001
            pass
        return _cmd_unsubscribe(phone, identity)
    # 2) Comandos sueltos — power users que ya saben qué quieren.
    if _RE_HELP.match(body):
        return _cmd_help()
    if _RE_INFO.match(body):
        return _cmd_info()
    if _RE_HUMAN.match(body):
        return _cmd_human(phone, identity)
    if _RE_THANKS.match(body):
        return _cmd_thanks()
    if _RE_KPIS.match(body):
        return _cmd_kpis()
    m = _RE_STATUS.match(body)
    if m:
        return _cmd_status(m.group(1))
    m = _RE_REAGENDAR.match(body)
    if m:
        return _cmd_reagendar(m.group(1), m.group(2), identity)
    m = _RE_MOTIVO.match(body)
    if m:
        return _cmd_motivo(m.group(1), m.group(2), m.group(3), identity)
    # 3) Agente conversacional (toma control si nada matcheó).
    try:
        from whatsapp_agent import handle as _agent_handle
        return _agent_handle(phone, body, profile_name, identity)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] agent falló: {e}")
        return None


# =============================================================================
# Endpoints
# =============================================================================
@router.post("/inbound")
async def webhook_inbound(
    request: Request,
    x_twilio_signature: Optional[str] = Header(default=None, alias="X-Twilio-Signature"),
):
    raw_body = await request.body()
    form = dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))

    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    public_url = os.environ.get("TWILIO_INBOUND_PUBLIC_URL", "").rstrip("/")
    if public_url:
        url = public_url + "/api/twilio/inbound"
    else:
        url = str(request.url)

    if auth_token and not _validate_signature(auth_token, url, form, x_twilio_signature):
        logger.warning(f"[twilio-inbound] firma inválida from={form.get('From')} url={url}")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # ----- Detectar STATUS CALLBACK (Twilio reporta entrega/error de un outbound) -----
    # Twilio manda al mismo webhook 2 cosas: (a) inbound real del usuario; (b) status
    # callback con MessageStatus + el SID del outbound original. Si es (b) actualizamos
    # el row outbound y no creamos un inbound nuevo (que apareceria con body vacio).
    msg_status = form.get("MessageStatus") or form.get("SmsStatus")
    twilio_sid = form.get("MessageSid") or form.get("SmsMessageSid")
    if msg_status and twilio_sid and not (form.get("Body") or "").strip():
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "UPDATE fpoc_notifications_log SET status = ? "
                    "WHERE twilio_sid = ? AND COALESCE(direction,'outbound') = 'outbound'",
                    (msg_status, twilio_sid),
                )
                cn.commit()
            logger.info(f"[twilio-inbound] status_callback sid={twilio_sid} status={msg_status}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[twilio-inbound] status_callback update fallo: {e}")
        return _twiml(None)

    from_number = _normalize_phone(form.get("From", ""))
    body = (form.get("Body") or "").strip()
    profile_name = form.get("ProfileName")
    num_media = int(form.get("NumMedia") or 0)
    media_urls = None
    if num_media > 0:
        urls = [form.get(f"MediaUrl{i}") for i in range(num_media) if form.get(f"MediaUrl{i}")]
        media_urls = "\n".join(urls) if urls else None

    logger.info(f"[twilio-inbound] from={from_number} body={body!r} sid={twilio_sid} media={num_media}")

    identity = _identify_phone(from_number)

    # Auto-onboarding: número desconocido → registrar como contacto + welcome.
    welcome = None
    if not identity["is_known"]:
        new_contact_id = _auto_onboard(from_number, profile_name)
        if new_contact_id:
            identity["contact_id"] = new_contact_id
            welcome = (
                f"¡Bienvenido{(' ' + profile_name) if profile_name else ''}! "
                "Quedaste suscrito a alertas. Escribe 'help' para ver comandos."
            )
            # Notificar al stream que un usuario NUEVO quedó conectado.
            try:
                from datetime import datetime as _dt
                from events import EVENTS
                from state import STATE
                EVENTS.emit(
                    "wa_user_onboarded",
                    STATE.sim_clock or _dt.utcnow(),
                    {
                        "phone": from_number,
                        "name": profile_name or from_number,
                        "kind": "contact",
                        "source": "inbound",
                        "contact_id": new_contact_id,
                    },
                )
            except Exception:  # noqa: BLE001
                pass

    _log_inbound(
        from_number=from_number,
        body=body,
        twilio_sid=twilio_sid,
        profile_name=profile_name,
        media_urls=media_urls,
        user_id=identity.get("user_id"),
        contact_id=identity.get("contact_id"),
        driver_id=identity.get("driver_id"),
    )

    # Dispatch a comando si aplica (cae al agente FSM si no matchea ninguno)
    reply = _dispatch(body, identity, from_number, profile_name)
    if reply is None and welcome is not None:
        reply = welcome
    # Si nada matcheó y el número ya estaba registrado, ack genérico
    if reply is None and identity.get("is_known"):
        reply = "Recibí tu mensaje. Escribí 'menu' para empezar o 'help' para comandos."
    return _twiml(reply)


@router.get("/inbound/test")
def webhook_test():
    """Sanity check del cableado, sin firma."""
    return {
        "status": "ok",
        "validate_signature": os.environ.get("TWILIO_INBOUND_VALIDATE_SIGNATURE", "true"),
        "auth_token_set": bool(os.environ.get("TWILIO_AUTH_TOKEN")),
        "public_url": os.environ.get("TWILIO_INBOUND_PUBLIC_URL", ""),
        "default_empresa_id": os.environ.get("TWILIO_INBOUND_DEFAULT_EMPRESA_ID", ""),
    }


# ----------------------------------------------------------------------------
# Alias path para configuraciones legacy en Twilio Console.
# Twilio puede estar apuntando a /api/v1/webhooks/twilio/whatsapp de un setup
# anterior; reusamos el mismo handler.
# ----------------------------------------------------------------------------
_legacy_router = APIRouter(prefix="/api/v1/webhooks/twilio", tags=["twilio-inbound"])


@_legacy_router.post("/whatsapp")
async def webhook_inbound_legacy(
    request: Request,
    x_twilio_signature: Optional[str] = Header(default=None, alias="X-Twilio-Signature"),
):
    return await webhook_inbound(request, x_twilio_signature)

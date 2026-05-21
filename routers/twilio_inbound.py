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

from core.db import get_conn


router = APIRouter(prefix="/api/twilio", tags=["twilio-inbound"])


# =============================================================================
# Helpers
# =============================================================================
def _mask_phone(phone: Optional[str]) -> str:
    """Devuelve '+56...XXXX' (últimos 4) para logging sin PII.

    Helper local — si en el futuro lo usan más módulos, moverlo a `core/security.py`.
    """
    if not phone:
        return "—"
    p = str(phone)
    if len(p) <= 4:
        return "***"
    return f"{p[:3]}...{p[-4:]}"


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
            "WHERE phone_e164 = ? AND activo = 1",
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
            "WHERE phone_e164 = ? AND active = 1",
            (phone_e164,),
        )
        r = cur.fetchone()
        if r is not None:
            out["contact_id"] = int(r[0])
            out["contact_empresa_id"] = int(r[1]) if r[1] is not None else None
            out["is_known"] = True

        cur.execute(
            "SELECT driver_id FROM fpoc_drivers "
            "WHERE phone_e164 = ? AND active = 1",
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
            cur.execute("SELECT empresa_id FROM fpoc_empresas_transporte WHERE activo = 1 ORDER BY empresa_id")
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
            try:
                cur.execute("SELECT last_insert_rowid()")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else None
            except Exception:
                return None
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
        try:
            cur.execute("SELECT last_insert_rowid()")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0


# =============================================================================
# Command parser
# =============================================================================
_RE_ACTIVAR = re.compile(r"^\s*ACTIVAR\s+([A-Z0-9]{6,16})\s*$", re.IGNORECASE)
_RE_STATUS = re.compile(r"^\s*status\s+(\S+)\s*$", re.IGNORECASE)
_RE_RUTA = re.compile(r"^\s*ruta\s+(R-\d+-\d+|\S+)\s*$", re.IGNORECASE)
_RE_FOLIO = re.compile(r"^\s*folio\s+(\S+)\s*$", re.IGNORECASE)
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
    """Status de una visita por tracking_id (=fpoc.simpli_visits.id como str).

    CR sync-bot-data: migrado de STATE.snapshot_df a fpoc.simpli_visits para
    que el bot muestre datos consistentes con el dashboard. Sin alarmas ML
    (`p_fallo` / `window_end`) — esos solo existen en el simulador ML.
    """
    tid = (tracking_id or "").strip()
    if not tid:
        return "Necesito un tracking_id para buscar."
    try:
        from core.db import get_conn as _gc
        with _gc() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT title, status, current_eta_cl, patente_falsa, planned_date
                FROM fpoc.simpli_visits
                WHERE CAST(id AS VARCHAR(32)) = ?
                """,
                (tid,),
            )
            r = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa] _cmd_status({tid}) DB fail: {e}")
        # Fallback defensivo: snapshot_df si lo hay (legacy TRK* sintéticos).
        from core.state import STATE
        if STATE.snapshot_df is None:
            return f"No pude consultar {tid} ahora mismo."
        df = STATE.snapshot_df
        matching = df[df["tracking_id"] == tid]
        if matching.empty:
            return f"No encuentro {tid}."
        row = matching.iloc[0]
        return (
            f"Visita {tid}\n"
            f"Cliente: {row['title']}\n"
            f"Vehículo: {row['vehicle_name']}\n"
            f"Estado: {row['status']}\n"
            f"ETA: {row['estimated_time_arrival']}"
        )
    if r is None:
        return f"No encuentro {tid}."
    title = r[0] or "—"
    status = r[1] or "pending"
    eta_raw = str(r[2]) if r[2] is not None else "—"
    eta = eta_raw[:16] if eta_raw and eta_raw != "—" else "—"
    patente = r[3]
    vehicle_name = f"PAT-{patente}" if patente is not None else "—"
    pd_str = str(r[4]) if r[4] is not None else "—"
    return (
        f"Visita {tid}\n"
        f"Cliente: {title}\n"
        f"Vehículo: {vehicle_name}\n"
        f"Estado: {status}\n"
        f"ETA: {eta}\n"
        f"Fecha plan: {pd_str}"
    )


def _cmd_folio(folio: str) -> str:
    """Busca un folio (reference) en simpli_visits, devuelve resumen WhatsApp."""
    folio_clean = folio.strip().lstrip("#").upper()
    # Acepta 'FAL-1001' (sacamos prefijo) o número directo
    num_part = folio_clean
    for prefix in ("FAL-", "FAL"):
        if num_part.startswith(prefix):
            num_part = num_part[len(prefix):]
            break
    try:
        ref_int = int(num_part)
    except ValueError:
        return f"Folio {folio} no parece un número válido. Formato: FAL-1234 o 14246784."
    try:
        from core.db import get_conn as _gc
        from datetime import date as _date_cls
        with _gc() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT id, title, comuna, region, ruta_id, driver_name, "
                "patente_falsa, status, current_eta_cl, planned_date, address "
                "FROM fpoc.simpli_visits WHERE reference = ? "
                "ORDER BY planned_date DESC "
                "LIMIT 1",
                ref_int,
            )
            r = cur.fetchone()
            if r is None:
                return f"Folio {folio_clean} no encontrado en visitas."
            tid = str(r.id)
            title = r.title or ""
            ruta_id = r.ruta_id or "—"
            driver = r.driver_name or "—"
            patente = str(r.patente_falsa) if r.patente_falsa is not None else "—"
            status = r.status or "pending"
            eta = str(r.current_eta_cl)[:16] if r.current_eta_cl else "—"
            pd = str(r.planned_date) if r.planned_date else "—"
            addr = r.address or "—"
            comuna = r.comuna or "—"
            region = r.region or "—"
            # Subfolios desde geo
            cur.execute(
                "SELECT COUNT(*) AS n FROM fpoc.geo_suborders WHERE parentorder = ?",
                ref_int,
            )
            n_sub = int(cur.fetchone().n or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa] _cmd_folio {folio} falló: {e}")
        return f"No pude leer el folio {folio_clean}."
    status_emoji = {"completed": "✅", "failed": "❌", "pending": "⏳"}.get(status, "•")
    lines = [
        f"📦 Folio {folio_clean}  {status_emoji} {status}",
        f"Cliente: {title}",
        f"Dirección: {addr}",
        f"{comuna} · {region}",
        "",
        f"Ruta: {ruta_id}",
        f"Driver: {driver} ({patente})",
        f"ETA: {eta}",
        f"Fecha plan: {pd}",
        f"Tracking: {tid}",
    ]
    if n_sub:
        lines.append(f"Subfolios: {n_sub}")
    return "\n".join(lines)


def _cmd_ruta(ruta_id: str) -> str:
    """Resumen WhatsApp de una ruta: R-YYYYMMDD-NNN → empresa, región, driver,
    stops, completadas, VIPs, folios, integridad."""
    try:
        from routers.rutas import get_ruta
        # Sintética: emulamos CurrentUser admin para WA (el agente ya hace identity)
        from core.auth import CurrentUser
        admin = CurrentUser(
            user_id=0, email="wa", display_name="WhatsApp",
            role="falabella_admin", empresa_id=None, empresa_nombre=None,
        )
        d = get_ruta(ruta_id=ruta_id, user=admin)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "no encontrada" in msg.lower() or "404" in msg:
            return f"No encuentro la ruta {ruta_id}. Formato: R-YYYYMMDD-NNN."
        logger.warning(f"[wa] _cmd_ruta {ruta_id} falló: {e}")
        return f"No pude leer la ruta {ruta_id}."
    lines = [
        f"📍 Ruta {d.ruta_id} ({d.planned_date})",
        f"Empresa: {d.empresa_nombre or '—'}",
        f"Región: {d.region or '—'}",
        f"Driver: {d.driver_name or '—'} ({d.patente or '—'})",
        "",
        f"Stops: {d.total_stops}  ·  OK {d.completed} · pend {d.pending} · fail {d.failed}",
        f"VIPs: {d.vip_count}",
        f"Folios: {d.folios_unicos}  ·  subfolios: {d.subfolios_total}",
    ]
    if not d.valid_routing:
        lines.append("")
        lines.append("⚠ Integridad:")
        for w in d.integrity_warnings:
            lines.append(f"  · {w}")
    # Primeros 3 VIP stops
    vip_stops = [s for s in d.stops if s.is_vip][:3]
    if vip_stops:
        lines.append("")
        lines.append("VIPs:")
        for s in vip_stops:
            lines.append(f"  · {s.cliente} ({s.comuna or '—'}) [{s.status}]")
    return "\n".join(lines)


def _cmd_reagendar(tracking_id: str, hh_mm: str, identity: dict) -> str:
    """POC: registramos un comment 'CLIENTE RECHAZA' con texto 'reagendado a HH:MM'."""
    try:
        from routers.comments import _persist_and_dispatch_comment
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
        from routers.comments import _persist_and_dispatch_comment, MOTIVOS_CATALOGO
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


def _is_admin_or_ops(identity: dict) -> bool:
    """True si el usuario está identificado como falabella_admin o falabella_ops.
    Estos roles bypass el gate de día operativo (testing/debug).

    Considera ambos shapes de identity:
      - twilio inbound: `user_role` (poblado por _identify_phone via fpoc_users)
      - agent_web: `role` (poblado por _identity_from_user con CurrentUser.role)
    """
    role = (identity.get("user_role") or identity.get("role") or "").lower()
    return role in ("falabella_admin", "falabella_ops")


def _day_not_active_reply() -> str:
    """Mensaje WhatsApp (<280 chars) cuando el bot rechaza queries operativas
    porque el día no está EN_CURSO/PAUSADO."""
    return (
        "El día operativo todavía no está iniciado. Cuando el equipo cargue el "
        "plan y arranque la jornada podrás consultar visitas, rutas y reportar "
        "motivos. Por ahora podés mandar:\n"
        "• help — ver comandos\n"
        "• humano — escalar a un coordinador\n"
        "• stop — darte de baja"
    )


def _cmd_kpis() -> str:
    """KPIs globales del día leídos de fpoc.simpli_visits.

    CR sync-bot-data: migrado de STATE.snapshot_df para coherencia con el
    dashboard. Se eliminan "Alertas anticipadas" (no existen en la fuente DB —
    son una feature exclusiva del simulador ML). Se agrega "Con problema"
    (status='failed') que sí está en la fuente real.
    """
    from sims._visits_db import kpis_today
    from datetime import date as _date_cls
    today = _date_cls.today().isoformat()
    k = kpis_today()
    if k["total"] == 0:
        # Fallback defensivo a snapshot_df si DB no tiene plan_date=today
        # (puede pasar en QA local con test data antigua).
        from core.state import STATE
        if STATE.snapshot_df is not None and len(STATE.snapshot_df) > 0:
            df = STATE.snapshot_df
            return (
                f"KPIs hoy ({STATE.today.isoformat() if STATE.today else today}):\n"
                f"• Visitas: {len(df)}\n"
                f"• Pendientes: {int((df['status'] == 'pending').sum())}\n"
                f"• Completadas: {int((df['status'] == 'completed').sum())}\n"
                "_(fuente: simulador — sin datos en DB para hoy)_"
            )
    return (
        f"KPIs hoy ({today}):\n"
        f"• Visitas: {k['total']}\n"
        f"• Pendientes: {k['pending']}\n"
        f"• Completadas: {k['completed']}\n"
        f"• Con problema: {k['failed']}"
    )


def _first_name(full: Optional[str], fallback: Optional[str]) -> str:
    """Primer token de un nombre completo. Si vacío, cae a fallback (ProfileName)
    o a 'tú' como último recurso. Útil para variables {{1}} de templates."""
    raw = (full or "").strip()
    if raw:
        return raw.split()[0]
    raw = (fallback or "").strip()
    if raw:
        return raw.split()[0]
    return "tú"


def _send_activation_template(
    *,
    phone: str,
    first_name: str,
    user_id: Optional[int],
    table: str,
    row_id: str,
    sender_to: Optional[str] = None,
) -> bool:
    """Envía el template `vd_cuenta_activada` (Content SID configurable por env).

    `sender_to` (dual-sender): si viene, el reply sale desde ese sender (el
    mismo al que el usuario escribió ACTIVAR). Si no, usa el default env.

    Devuelve True si el envío salió sin excepción, False en caso contrario.
    `send_whatsapp` ya hace su propio logging de la entrega/error a través del
    notifications_log, así que acá solo loggeamos a nivel INFO para correlación.
    """
    from core.twilio_templates import cuenta_activada_sid
    content_sid = cuenta_activada_sid()
    try:
        from routers.notifications import send_whatsapp
        send_whatsapp(
            content_sid=content_sid,
            content_variables={"1": first_name},
            targets=[(user_id, phone)],
            subject="Cuenta activada",
            triggered_by="activation",
            from_number=sender_to,
        )
        logger.info(
            f"[activation] template vd_cuenta_activada enviado a phone={_mask_phone(phone)} "
            f"for {table}={row_id}"
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] vd_cuenta_activada falló: {e}")
        return False


def _cmd_activar(
    token: str,
    phone: str,
    profile_name: Optional[str],
    sender_to: Optional[str] = None,
) -> Optional[str]:
    """CR-014 / hotfix: activación user-initiated por wa.me link.

    Busca el token en fpoc_users / fpoc_drivers / fpoc_empresa_contactos (en ese
    orden de prioridad — un user con login mandando ACTIVAR es más probable que
    un driver, y ambos más probables que un contacto). El primer match con
    activation_used_at IS NULL gana.

    Side-effects sobre el row encontrado:
      - phone_e164 = <from_phone> (sobreescribe el placeholder con el real)
      - activation_used_at = CURRENT_TIMESTAMP
      - users: activo=1, notify_whatsapp=1
      - drivers: active=1, notify_whatsapp=1, opted_in_at=CURRENT_TIMESTAMP
      - contactos: active=1, opted_in_at=CURRENT_TIMESTAMP

    Reply en caso de MATCH:
      - El "✅ Cuenta activada" se envía via **template aprobado**
        `vd_cuenta_activada` (Content SID en env `TWILIO_CONTENT_SID_CUENTA_ACTIVADA`).
        Esto destraba el error 63112 para senders en warmup (Tier 0) cuyos
        usuarios nuevos no tienen ventana 24h abierta. Devolvemos `None` para
        que `webhook_inbound` NO mande otro freeform encima.
      - Fallback freeform si el template no está approved aún o falla algo en
        el envío: devolvemos el string viejo y dejamos que el caller lo mande
        por freeform (puede pegar 63112 pero al menos lo intentamos y la DB
        ya quedó actualizada).

    Reply en caso de NO MATCH: string freeform tal cual antes. Edge case menor
    (token inválido + usuario nuevo → 63112), aceptado para POC.
    """
    token_norm = token.strip().upper()
    if not token_norm:
        return "No reconocí ese código. Pedile al admin un nuevo link de activación."
    try:
        with get_conn() as cn:
            cur = cn.cursor()

            # 1) users
            cur.execute(
                "SELECT user_id, display_name FROM fpoc_users "
                "WHERE activation_token = ? AND activation_used_at IS NULL",
                (token_norm,),
            )
            r = cur.fetchone()
            if r is not None:
                uid = int(r[0])
                display_name = str(r[1] or "").strip()
                cur.execute(
                    "UPDATE fpoc_users SET phone_e164 = ?, notify_whatsapp = 1, "
                    "activo = 1, activation_used_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = ?",
                    (phone, uid),
                )
                cn.commit()
                logger.info(f"[twilio-inbound] ACTIVAR token={token_norm} matched user_id={uid} phone={_mask_phone(phone)}")
                first = _first_name(display_name, profile_name)
                if _send_activation_template(
                    phone=phone, first_name=first, user_id=uid,
                    table="user_id", row_id=str(uid),
                    sender_to=sender_to,
                ):
                    return None
                return f"✅ Cuenta activada, {first}! Mandá 'menu' para empezar."

            # 2) drivers
            cur.execute(
                "SELECT driver_id, name FROM fpoc_drivers "
                "WHERE activation_token = ? AND activation_used_at IS NULL",
                (token_norm,),
            )
            r = cur.fetchone()
            if r is not None:
                drv_id = str(r[0])
                name = str(r[1] or "").strip()
                cur.execute(
                    "UPDATE fpoc_drivers SET phone_e164 = ?, notify_whatsapp = 1, "
                    "opted_in_at = CURRENT_TIMESTAMP, active = 1, "
                    "activation_used_at = CURRENT_TIMESTAMP "
                    "WHERE driver_id = ?",
                    (phone, drv_id),
                )
                cn.commit()
                logger.info(f"[twilio-inbound] ACTIVAR token={token_norm} matched driver_id={drv_id} phone={_mask_phone(phone)}")
                first = _first_name(name, profile_name)
                if _send_activation_template(
                    phone=phone, first_name=first, user_id=None,
                    table="driver_id", row_id=drv_id,
                    sender_to=sender_to,
                ):
                    return None
                return f"✅ Cuenta activada, {first}! Mandá 'menu' para empezar."

            # 3) empresa_contactos
            cur.execute(
                "SELECT contact_id, nombre FROM fpoc_empresa_contactos "
                "WHERE activation_token = ? AND activation_used_at IS NULL",
                (token_norm,),
            )
            r = cur.fetchone()
            if r is not None:
                cid = int(r[0])
                nombre = str(r[1] or "").strip()
                cur.execute(
                    "UPDATE fpoc_empresa_contactos SET phone_e164 = ?, "
                    "opted_in_at = CURRENT_TIMESTAMP, active = 1, "
                    "activation_used_at = CURRENT_TIMESTAMP, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE contact_id = ?",
                    (phone, cid),
                )
                cn.commit()
                logger.info(f"[twilio-inbound] ACTIVAR token={token_norm} matched contact_id={cid} phone={_mask_phone(phone)}")
                first = _first_name(nombre, profile_name)
                if _send_activation_template(
                    phone=phone, first_name=first, user_id=None,
                    table="contact_id", row_id=str(cid),
                    sender_to=sender_to,
                ):
                    return None
                return f"✅ Cuenta activada, {first}! Mandá 'menu' para empezar."
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] ACTIVAR {token_norm} falló: {e}")
        return "Tuvimos un problema activando tu cuenta. Pedile al admin un nuevo link."

    logger.info(f"[twilio-inbound] ACTIVAR token={token_norm} no match (o ya usado) phone={_mask_phone(phone)}")
    return "No reconocí ese código. Pedile al admin un nuevo link de activación."


def _dispatch(
    body: str,
    identity: dict,
    phone: str,
    profile_name: Optional[str] = None,
    sender_to: Optional[str] = None,
) -> Optional[str]:
    """Devuelve respuesta TwiML o None si no hay match.

    Prioridades:
      1) Compliance: opt-out (stop) — siempre gana.
      2) Comandos sueltos (power users): status/motivo/kpis/help/etc.
      3) Agente conversacional FSM (whatsapp_agent.handle).

    `sender_to`: dual-sender. Es el número Twilio AL QUE el usuario escribió
    (i.e. nuestro sender). Se propaga a comandos que disparan envíos outbound
    (p.ej. `_cmd_activar` → template `vd_cuenta_activada`) para que el reply
    salga desde el mismo sender. Si es None, los envíos caen al default env var.
    """
    if not body:
        return None
    # 0) CR-014: activación por wa.me link. Va ANTES de cualquier otro check
    # porque puede ser el primerísimo mensaje del usuario y necesita abrir la
    # ventana 24h sin pasar por templates ni FSM. Si matchea, devolvemos
    # freeform y listo.
    m = _RE_ACTIVAR.match(body)
    if m:
        return _cmd_activar(m.group(1), phone, profile_name, sender_to=sender_to)
    # 1) Compliance: opt-out manda y termina cualquier sesión activa.
    if _RE_STOP.match(body):
        try:
            from sims.whatsapp_agent import Session as _WaSession
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
    # 2.5) Day-state gate (CR-015): a partir de acá los comandos exponen data
    # operativa (snapshot del simulador o lecturas de simpli_visits). Si el día
    # NO está EN_CURSO/PAUSADO y el usuario no es admin/ops, rechazamos para
    # no mostrar data sintética como real. stop/help/info/humano/thanks/activar
    # ya pasaron arriba y son compliance/meta — no se gatean.
    from core.state import is_operational_day_active
    if not _is_admin_or_ops(identity) and not is_operational_day_active():
        return _day_not_active_reply()
    # CR-016: los comandos operativos (status, folio, ruta, kpis, motivo,
    # reagendar) YA NO matchean por regex acá. Se delegan al agente híbrido
    # (`sims.whatsapp_agent.handle` → `sims.llm_agent.chat` con tool-calling).
    # Eso destraba lenguaje natural ("cómo va mi ruta?" en lugar de "ruta X")
    # y tolera typos. Las regex (_RE_STATUS, _RE_RUTA, etc.) quedan declaradas
    # en este módulo por si en el futuro queremos reactivar atajos directos,
    # pero NO se evalúan en el flow principal.
    # 3) Agente conversacional híbrido (FSM en sesión activa + LLM en idle).
    try:
        from sims.whatsapp_agent import handle as _agent_handle
        return _agent_handle(phone, body, profile_name, identity)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-inbound] agent falló: {e}")
        return None


# =============================================================================
# Status callback helper
# =============================================================================
def _apply_status_callback(twilio_sid: str, msg_status: str) -> None:
    """Persiste el status reportado por Twilio sobre un outbound.

    Twilio dispara este callback sobre `Status Callback URL` (configurado en
    el sandbox). Tambien lo manda al `When a message comes in` con body vacio
    — ahi tambien lo capturamos en /inbound antes de procesar como inbound.
    """
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc_notifications_log SET status = ? "
                "WHERE twilio_sid = ? AND COALESCE(direction,'outbound') = 'outbound'",
                (msg_status, twilio_sid),
            )
            cn.commit()
        logger.info(f"[twilio-status] sid={twilio_sid} status={msg_status}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[twilio-status] update fallo: {e}")


# =============================================================================
# Endpoints
# =============================================================================
@router.post("/status")
async def webhook_status(
    request: Request,
    x_twilio_signature: Optional[str] = Header(default=None, alias="X-Twilio-Signature"),
):
    """Endpoint dedicado para Status Callback de Twilio.

    Twilio acepta dos webhooks distintos en el sandbox:
      - When a message comes in -> /api/twilio/inbound
      - Status Callback URL     -> /api/twilio/status

    Ambos validan firma y son idempotentes. Devolvemos siempre 200 con TwiML
    vacio para que Twilio no reintente.
    """
    raw_body = await request.body()
    form = dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))

    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    public_url = os.environ.get("TWILIO_INBOUND_PUBLIC_URL", "").rstrip("/")
    url = (public_url + "/api/twilio/status") if public_url else str(request.url)
    if auth_token and not _validate_signature(auth_token, url, form, x_twilio_signature):
        logger.warning(f"[twilio-status] firma invalida sid={form.get('MessageSid')}")
        raise HTTPException(status_code=403, detail="Invalid signature")

    msg_status = form.get("MessageStatus") or form.get("SmsStatus")
    twilio_sid = form.get("MessageSid") or form.get("SmsMessageSid")
    if msg_status and twilio_sid:
        _apply_status_callback(twilio_sid, msg_status)
    return _twiml(None)


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
        _apply_status_callback(twilio_sid, msg_status)
        return _twiml(None)

    # Twilio "From" = usuario que escribe; "To" = nuestro sender (al que escribió).
    # Para dual-sender, capturamos `sender_to` para que el reply salga del MISMO
    # número al que el usuario escribió (no del default env var). Si "To" viene
    # vacío (caso raro, no debería pasar en webhooks reales pero sí en algunos
    # tests/curls manuales), `sender_to` queda como "" y _normalize_from_number
    # lo trata como None → fallback al TWILIO_WHATSAPP_FROM default.
    from_number = _normalize_phone(form.get("From", ""))
    sender_to_raw = form.get("To", "")
    sender_to = _normalize_phone(sender_to_raw) if sender_to_raw else ""
    body = (form.get("Body") or "").strip()
    profile_name = form.get("ProfileName")
    num_media = int(form.get("NumMedia") or 0)
    media_urls = None
    if num_media > 0:
        urls = [form.get(f"MediaUrl{i}") for i in range(num_media) if form.get(f"MediaUrl{i}")]
        media_urls = "\n".join(urls) if urls else None

    logger.info(
        f"[twilio-inbound] from={from_number} to={sender_to or '(empty)'} "
        f"body={body!r} sid={twilio_sid} media={num_media}"
    )

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
                from core.events import EVENTS
                from core.state import STATE
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
    reply = _dispatch(body, identity, from_number, profile_name, sender_to=sender_to or None)
    if reply is None and welcome is not None:
        reply = welcome
    # Si nada matcheó y el número ya estaba registrado, ack genérico
    if reply is None and identity.get("is_known"):
        reply = "Recibí tu mensaje. Escribí 'menu' para empezar o 'help' para comandos."

    # WhatsApp Business Senders en Twilio NO aceptan TwiML inline para
    # responder al inbound (a diferencia del sandbox). Hay que mandar el
    # reply via outbound API. Solo respondemos TwiML vacío (200 OK) para
    # acusar recibo a Twilio.
    if reply:
        try:
            from routers.notifications import send_whatsapp
            send_whatsapp(
                body=reply,
                targets=[(identity.get("user_id"), from_number)],
                subject="Respuesta agente",
                triggered_by="agent_reply",
                # Dual-sender: el reply sale del MISMO sender al que el usuario
                # escribió. Si sender_to viene vacío (raro), cae al default env.
                from_number=sender_to or None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[twilio_inbound] reply via outbound API falló: {e}")
            # Fallback a TwiML por las dudas — en sandbox sí funciona
            return _twiml(reply)
    return _twiml(None)


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

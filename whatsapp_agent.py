"""Agente conversacional WhatsApp con FSM.

Estado por número de teléfono persistido en `fpoc_whatsapp_sessions` (TTL 30 min).
Conviven dos modos:
  - Menú interactivo (este módulo) — flujo guiado paso a paso
  - Comandos sueltos (ver twilio_inbound._dispatch) — power users

Si el usuario manda un comando reconocido, twilio_inbound lo maneja antes y la
sesión se preserva. Si no matchea ningún comando, llega acá.

Estados:
  idle                       inicial / sin sesión
  awaiting_role              esperando 1/2/3 (driver/cliente/operador)
  awaiting_driver_id         pidiendo driver_id o RUT (cuando phone no matchea)
  menu_driver                menú principal del conductor
  awaiting_tracking          pidiendo tracking_id para reportar
  choosing_motivo            mostrando lista de motivos
  awaiting_comentario        capturando comentario libre
  done_motivo                cerrando flujo de reporte
  awaiting_client_tracking   cliente pidiendo info de su pedido
  menu_cliente               cliente con tracking ya cargado
  awaiting_reagenda          cliente pide nueva ventana
  awaiting_client_comment    cliente deja mensaje para el conductor
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from db import get_conn


SESSION_TTL_MINUTES = 30


# =============================================================================
# Sesión
# =============================================================================
@dataclass
class Session:
    phone: str
    state: str = "idle"
    role: Optional[str] = None
    identified_id: Optional[str] = None
    context: dict = field(default_factory=dict)
    updated_at: Optional[str] = None

    @classmethod
    def load(cls, phone: str) -> "Session":
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT phone_e164, state, role, identified_id, context, updated_at "
                "FROM fpoc_whatsapp_sessions WHERE phone_e164 = ?",
                (phone,),
            )
            r = cur.fetchone()
        if r is None:
            return cls(phone=phone)
        # Expiración por TTL
        try:
            ts = datetime.fromisoformat(str(r[5]).replace("Z", "+00:00").split(".")[0])
        except Exception:  # noqa: BLE001
            ts = datetime.utcnow()
        if datetime.utcnow() - ts > timedelta(minutes=SESSION_TTL_MINUTES):
            cls.delete(phone)
            return cls(phone=phone)
        ctx = {}
        if r[4]:
            try:
                ctx = json.loads(r[4])
            except Exception:  # noqa: BLE001
                ctx = {}
        return cls(
            phone=str(r[0]),
            state=str(r[1] or "idle"),
            role=str(r[2]) if r[2] else None,
            identified_id=str(r[3]) if r[3] else None,
            context=ctx,
            updated_at=str(r[5]) if r[5] else None,
        )

    def save(self) -> None:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                INSERT INTO fpoc_whatsapp_sessions
                  (phone_e164, state, role, identified_id, context, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(phone_e164) DO UPDATE SET
                  state = excluded.state,
                  role = excluded.role,
                  identified_id = excluded.identified_id,
                  context = excluded.context,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self.phone,
                    self.state,
                    self.role,
                    self.identified_id,
                    json.dumps(self.context) if self.context else None,
                ),
            )
            cn.commit()

    def reset(self) -> None:
        self.state = "idle"
        self.role = None
        self.identified_id = None
        self.context = {}

    @staticmethod
    def delete(phone: str) -> None:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("DELETE FROM fpoc_whatsapp_sessions WHERE phone_e164 = ?", (phone,))
            cn.commit()


# =============================================================================
# Lookups
# =============================================================================
def _find_driver_by_phone(phone: str) -> Optional[dict]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT driver_id, name, vehicle_id, vehicle_name, phone_e164 "
            "FROM fpoc_drivers WHERE phone_e164 = ? AND active = 1 LIMIT 1",
            (phone,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {"driver_id": r[0], "name": r[1], "vehicle_id": int(r[2]) if r[2] else None,
            "vehicle_name": r[3], "phone_e164": r[4]}


def _find_driver_by_id_or_rut(token: str) -> Optional[dict]:
    """Acepta driver_id (D-001), RUT (12345678-9), o vehicle_id (22)."""
    t = token.strip().upper().replace(".", "").replace("-", "")
    with get_conn() as cn:
        cur = cn.cursor()
        # match por driver_id (case insensitive, sin guiones/puntos)
        cur.execute(
            "SELECT driver_id, name, vehicle_id, vehicle_name "
            "FROM fpoc_drivers "
            "WHERE active = 1 AND ("
            "  REPLACE(REPLACE(UPPER(driver_id), '-', ''), '.', '') = ? "
            "  OR CAST(vehicle_id AS TEXT) = ?"
            ") LIMIT 1",
            (t, token.strip()),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {"driver_id": r[0], "name": r[1], "vehicle_id": int(r[2]) if r[2] else None,
            "vehicle_name": r[3]}


def _visits_for_vehicle(vehicle_id: int) -> list[dict]:
    """Visitas del día actual del snapshot, ordenadas por order asc."""
    from state import STATE
    if STATE.snapshot_df is None:
        return []
    df = STATE.snapshot_df[STATE.snapshot_df["vehicle_id"] == vehicle_id]
    df = df.sort_values("order")
    return [
        {
            "tracking_id": str(r["tracking_id"]),
            "title": str(r["title"]),
            "comuna": str(r.get("comuna", "")),
            "status": str(r["status"]),
            "window_end": str(r["window_end"]),
            "eta": str(r["estimated_time_arrival"]),
            "p_fallo": float(r["p_fallo"]),
            "alert_valuedata": bool(r["alert_valuedata"]),
            "order": int(r["order"]),
        }
        for _, r in df.iterrows()
    ]


def _visit_by_tracking(tracking_id: str) -> Optional[dict]:
    from state import STATE
    if STATE.snapshot_df is None:
        return None
    df = STATE.snapshot_df[STATE.snapshot_df["tracking_id"] == tracking_id]
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "tracking_id": str(r["tracking_id"]),
        "title": str(r["title"]),
        "address": str(r.get("address", "")),
        "comuna": str(r.get("comuna", "")),
        "vehicle_id": int(r["vehicle_id"]),
        "vehicle_name": str(r["vehicle_name"]),
        "status": str(r["status"]),
        "window_end": str(r["window_end"]),
        "eta": str(r["estimated_time_arrival"]),
        "p_fallo": float(r["p_fallo"]),
    }


# =============================================================================
# Motivos para menú (los 6 más frecuentes según uso operacional)
# =============================================================================
MENU_MOTIVOS = [
    "SIN MORADORES",
    "CLIENTE RECHAZA",
    "PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN",
    "PRODUCTO CON PROBLEMAS",
    "SINIESTRO EN CALLE",
    "PRODUCTO ROBADO",
]


# =============================================================================
# Renders
# =============================================================================
def _render_role_menu(profile_name: Optional[str]) -> str:
    saludo = f"Hola {profile_name}" if profile_name else "Hola"
    return (
        f"👋 {saludo}! Soy el asistente Falabella ValueData.\n"
        "¿Quién eres?\n"
        " 1️⃣  Soy conductor\n"
        " 2️⃣  Soy cliente\n"
        " 3️⃣  Soy operador\n\n"
        "Tip: en cualquier momento podés escribir 'menu' para volver acá, "
        "o 'salir' para terminar."
    )


def _render_driver_menu(driver: dict) -> str:
    return (
        f"Hola {driver['name']} 👋  ({driver['vehicle_name']})\n"
        "¿Qué necesitás?\n"
        " 1️⃣  Ver mi ruta de hoy\n"
        " 2️⃣  Próxima visita pendiente\n"
        " 3️⃣  Reportar incidente / motivo\n"
        " 4️⃣  Hablar con coordinador\n"
        " 9️⃣  Salir"
    )


def _render_route(driver: dict) -> str:
    if driver.get("vehicle_id") is None:
        return "No tenés vehículo asignado para hoy."
    visits = _visits_for_vehicle(driver["vehicle_id"])
    if not visits:
        return "No tenés visitas asignadas en el snapshot actual."
    pending = [v for v in visits if v["status"] == "pending"]
    completed = [v for v in visits if v["status"] == "completed"]
    alerts = [v for v in pending if v["alert_valuedata"]]
    head = (
        f"Tu ruta hoy ({driver['vehicle_name']}):\n"
        f"• Total: {len(visits)}\n"
        f"• Completadas: {len(completed)}\n"
        f"• Pendientes: {len(pending)}\n"
        f"• Alertas anticipadas: {len(alerts)}\n"
    )
    if alerts:
        head += "\n⚠️ En riesgo:\n"
        for v in alerts[:5]:
            head += f"  · {v['tracking_id']} — {v['title'][:30]} ({int(v['p_fallo']*100)}%)\n"
    return head + "\nEscribí '3' para reportar una visita, '2' para la próxima, o 'menu'."


def _render_next_visit(driver: dict) -> str:
    if driver.get("vehicle_id") is None:
        return "No tenés vehículo asignado."
    pending = [v for v in _visits_for_vehicle(driver["vehicle_id"]) if v["status"] == "pending"]
    if not pending:
        return "No tenés visitas pendientes 🎉. Mandá 'menu' para volver."
    v = pending[0]
    risk = "🟢" if v["p_fallo"] < 0.3 else ("🟡" if v["p_fallo"] < 0.5 else "🔴")
    return (
        f"Próxima visita:\n"
        f"  {v['tracking_id']} — {v['title']}\n"
        f"  Comuna: {v['comuna']}\n"
        f"  ETA: {v['eta'][:5]}  ·  Window end: {v['window_end'][:5]}\n"
        f"  Riesgo: {risk} {int(v['p_fallo']*100)}%\n\n"
        "¿Querés reportar algo de esta visita?\n"
        "  Mandá 's' para sí, '2' para ver el resumen de la ruta, o 'menu'."
    )


def _render_motivo_menu(visit: dict) -> str:
    risk_pct = int(visit["p_fallo"] * 100)
    risk = "🟢" if risk_pct < 30 else ("🟡" if risk_pct < 50 else "🔴")
    head = (
        f"Visita: {visit['title']}\n"
        f"  {visit['comuna']} — Window {visit['window_end'][:5]}\n"
        f"  ETA: {visit['eta'][:5]}  · Riesgo: {risk} {risk_pct}%\n\n"
        "Elegí motivo:\n"
    )
    for i, m in enumerate(MENU_MOTIVOS, 1):
        head += f" {i}️⃣  {m}\n"
    head += " 0️⃣  Cancelar"
    return head


def _render_done_motivo(tracking_id: str, motivo: str) -> str:
    return (
        f"✅ Registrado.\n"
        f"  {tracking_id} → {motivo}\n"
        "Tu coordinador fue notificado.\n\n"
        "¿Algo más?\n"
        " 1️⃣  Reportar otra visita\n"
        " 2️⃣  Ver mi ruta\n"
        " 9️⃣  Salir"
    )


def _render_client_intro() -> str:
    return (
        "Para consultar el estado de tu pedido o reagendar, mandame el código "
        "de seguimiento (ej: TRK0600009).\n"
        "Mandá 'menu' para volver al inicio."
    )


def _render_client_visit(visit: dict) -> str:
    return (
        f"Pedido {visit['tracking_id']}\n"
        f"  Cliente: {visit['title']}\n"
        f"  Dirección: {visit['address']}\n"
        f"  Estado: {visit['status']}\n"
        f"  ETA: {visit['eta'][:5]}  ·  Hasta: {visit['window_end'][:5]}\n\n"
        "¿Qué querés hacer?\n"
        " 1️⃣  Confirmo, voy a estar\n"
        " 2️⃣  Reagendar a otra hora\n"
        " 3️⃣  Dejar mensaje al conductor\n"
        " 9️⃣  Salir"
    )


# =============================================================================
# Handler principal del agente
# =============================================================================
def handle(phone: str, body: str, profile_name: Optional[str], identity: dict) -> Optional[str]:
    """Dispatch del FSM. Retorna mensaje para enviar o None si el agente no quiere
    tomar el control (ej. body parece comando legacy).
    """
    text = (body or "").strip()
    text_lower = text.lower()

    # Comandos universales (siempre disponibles dentro del flujo)
    if text_lower in ("menu", "menú", "inicio", "volver", "start", "hola"):
        s = Session(phone=phone, state="awaiting_role")
        s.save()
        return _render_role_menu(profile_name)
    if text_lower in ("salir", "exit", "bye", "chao"):
        Session.delete(phone)
        return "👋 Listo, conversación cerrada. Mandá 'hola' para empezar de nuevo."

    sess = Session.load(phone)

    # Si no hay sesión activa o el usuario es nuevo: arrancamos en awaiting_role
    if sess.state == "idle":
        # Auto-detect driver por phone → directo al menú driver
        driver = _find_driver_by_phone(phone)
        if driver:
            sess.state = "menu_driver"
            sess.role = "driver"
            sess.identified_id = driver["driver_id"]
            sess.context = {"driver": driver}
            sess.save()
            return _render_driver_menu(driver) + "\n\n(detecté tu número en el sistema)"
        sess.state = "awaiting_role"
        sess.save()
        return _render_role_menu(profile_name)

    # Dispatch por estado
    handler_fn = _STATE_HANDLERS.get(sess.state)
    if handler_fn is None:
        # Estado corrupto → reset
        sess.reset()
        sess.save()
        return _render_role_menu(profile_name)
    return handler_fn(sess, text, text_lower, identity)


# =============================================================================
# Handlers por estado
# =============================================================================
def _on_awaiting_role(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    if text == "1" or "conductor" in text_lower:
        # Antes de pedir ID, intentamos auto-match por phone
        driver = _find_driver_by_phone(sess.phone)
        if driver:
            sess.state = "menu_driver"
            sess.role = "driver"
            sess.identified_id = driver["driver_id"]
            sess.context = {"driver": driver}
            sess.save()
            return _render_driver_menu(driver)
        sess.state = "awaiting_driver_id"
        sess.role = "driver"
        sess.save()
        return (
            "Para identificarte, decime tu RUT (sin puntos, ej 12345678-9) o tu ID de\n"
            "conductor (D-001) o el ID de tu vehículo (1-12). 0 para cancelar:"
        )
    if text == "2" or "cliente" in text_lower:
        sess.state = "awaiting_client_tracking"
        sess.role = "cliente"
        sess.save()
        return _render_client_intro()
    if text == "3" or "operador" in text_lower:
        sess.state = "idle"
        sess.role = "operador"
        sess.save()
        return (
            "Como operador podés usar comandos directos:\n"
            "  status TRK..., kpis, motivo TRK... <MOTIVO>: <comentario>, help"
        )
    return "No entendí. Elegí 1 (conductor), 2 (cliente) o 3 (operador). 'menu' para volver."


def _on_awaiting_driver_id(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    if text == "0":
        sess.reset()
        sess.save()
        return _render_role_menu(None)
    driver = _find_driver_by_id_or_rut(text)
    if driver is None:
        return (
            f"No encuentro '{text}' en el sistema. Probá con tu RUT (ej 12345678-9), "
            "tu driver_id (D-001) o el ID de tu vehículo (1-12). '0' para cancelar."
        )
    sess.state = "menu_driver"
    sess.identified_id = driver["driver_id"]
    sess.context = {"driver": driver}
    sess.save()
    return _render_driver_menu(driver)


def _on_menu_driver(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    driver = sess.context.get("driver")
    if not driver:
        sess.reset()
        sess.save()
        return _render_role_menu(None)
    if text == "1" or "ruta" in text_lower:
        return _render_route(driver) + "\n"
    if text == "2" or "próxima" in text_lower or "proxima" in text_lower or "siguiente" in text_lower:
        return _render_next_visit(driver)
    if text == "3" or "reportar" in text_lower or "incidente" in text_lower:
        sess.state = "awaiting_tracking"
        sess.save()
        return "Decime el tracking_id (ej: TRK0600009) o '0' para cancelar:"
    if text == "4" or "coordinador" in text_lower or "humano" in text_lower:
        sess.reset()
        sess.save()
        return (
            "✋ Te escalé a un coordinador. En unos minutos te contactan.\n"
            "Si es urgente, llamá al call center."
        )
    if text == "9" or text_lower in ("salir", "exit"):
        Session.delete(sess.phone)
        return "👋 Hasta luego."
    if text_lower == "s" or text_lower == "si" or text_lower == "sí":
        # Atajo desde "próxima visita" → reportar la primera pendiente
        pending = [v for v in _visits_for_vehicle(driver["vehicle_id"]) if v["status"] == "pending"]
        if pending:
            sess.state = "choosing_motivo"
            sess.context["tracking_id"] = pending[0]["tracking_id"]
            sess.save()
            visit = _visit_by_tracking(pending[0]["tracking_id"])
            return _render_motivo_menu(visit)
    return "No entendí. Elegí 1, 2, 3, 4 o 9. 'menu' para volver."


def _on_awaiting_tracking(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    if text == "0":
        driver = sess.context.get("driver")
        sess.state = "menu_driver"
        sess.save()
        return _render_driver_menu(driver) if driver else _render_role_menu(None)
    visit = _visit_by_tracking(text.upper().strip())
    if visit is None:
        return f"No encuentro {text}. Pegá el tracking_id completo (ej TRK0600009) o '0' para cancelar."
    sess.state = "choosing_motivo"
    sess.context["tracking_id"] = visit["tracking_id"]
    sess.save()
    return _render_motivo_menu(visit)


def _on_choosing_motivo(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    if text == "0":
        driver = sess.context.get("driver")
        sess.state = "menu_driver"
        sess.context.pop("tracking_id", None)
        sess.save()
        return _render_driver_menu(driver) if driver else _render_role_menu(None)
    if not text.isdigit():
        return "Elegí un número del 1 al 6, o '0' para cancelar."
    idx = int(text)
    if idx < 1 or idx > len(MENU_MOTIVOS):
        return f"Número fuera de rango. Elegí del 1 al {len(MENU_MOTIVOS)}."
    motivo = MENU_MOTIVOS[idx - 1]
    sess.state = "awaiting_comentario"
    sess.context["motivo"] = motivo
    sess.save()
    return f"Elegiste: {motivo}\nAgregá un comentario corto (o escribí 'skip'):"


def _on_awaiting_comentario(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    tid = sess.context.get("tracking_id")
    motivo = sess.context.get("motivo")
    if not tid or not motivo:
        sess.reset()
        sess.save()
        return "Algo salió mal con el flujo, volvé a empezar mandando 'menu'."
    comentario = "" if text_lower == "skip" else text
    if not comentario:
        comentario = f"(sin comentario adicional, reportado via WhatsApp por {sess.identified_id or sess.phone})"
    try:
        from comments import _persist_and_dispatch_comment
        actor = sess.identified_id or sess.phone
        _persist_and_dispatch_comment(
            tracking_id=tid,
            motivo=motivo,
            comentario=comentario,
            user_id=identity.get("user_id"),
            user_display_name=str(actor),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa-agent] persist falló: {e}")
        sess.state = "menu_driver"
        sess.context.pop("tracking_id", None)
        sess.context.pop("motivo", None)
        sess.save()
        return f"❌ No pude registrar el comentario: {e}\n\nMandá 'menu'."
    sess.state = "done_motivo"
    sess.context.pop("motivo", None)
    sess.save()
    return _render_done_motivo(tid, motivo)


def _on_done_motivo(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    if text == "1":
        sess.state = "awaiting_tracking"
        sess.context.pop("tracking_id", None)
        sess.save()
        return "Decime el tracking_id de la siguiente visita o '0' para cancelar:"
    if text == "2":
        driver = sess.context.get("driver")
        sess.state = "menu_driver"
        sess.context.pop("tracking_id", None)
        sess.save()
        return _render_driver_menu(driver) if driver else _render_role_menu(None)
    if text == "9" or text_lower in ("salir", "exit"):
        Session.delete(sess.phone)
        return "👋 Gracias, hasta luego."
    return "Elegí 1, 2 o 9."


def _on_awaiting_client_tracking(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    visit = _visit_by_tracking(text.upper().strip())
    if visit is None:
        return f"No encuentro {text}. Pegá el código completo (ej TRK0600009) o 'menu' para volver."
    sess.state = "menu_cliente"
    sess.context["tracking_id"] = visit["tracking_id"]
    sess.save()
    return _render_client_visit(visit)


def _on_menu_cliente(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    tid = sess.context.get("tracking_id")
    if not tid:
        sess.reset()
        sess.save()
        return _render_role_menu(None)
    if text == "1":
        # Confirmar presencia → registramos un comment informativo
        try:
            from comments import _persist_and_dispatch_comment
            _persist_and_dispatch_comment(
                tracking_id=tid,
                motivo="SIN MORADORES",  # placeholder catalog-valido; el comentario aclara
                comentario="Cliente confirmó por WhatsApp que estará presente",
                user_id=identity.get("user_id"),
                user_display_name=f"cliente {sess.phone}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wa-agent] cliente confirma falló: {e}")
        Session.delete(sess.phone)
        return "✅ Genial, gracias por confirmar. El conductor fue notificado."
    if text == "2":
        sess.state = "awaiting_reagenda"
        sess.save()
        return "¿Para qué hora te queda mejor? (HH:MM, ej 16:30)"
    if text == "3":
        sess.state = "awaiting_client_comment"
        sess.save()
        return "Escribí tu mensaje para el conductor (máx 200 chars):"
    if text == "9" or text_lower in ("salir", "exit"):
        Session.delete(sess.phone)
        return "👋 Listo, hasta luego."
    return "Elegí 1, 2, 3 o 9."


def _on_awaiting_reagenda(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    tid = sess.context.get("tracking_id")
    m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
    if not m:
        return "Formato inválido. Escribí HH:MM (ej 16:30) o 'menu' para cancelar."
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return "Hora fuera de rango. Probá otra vez."
    try:
        from comments import _persist_and_dispatch_comment
        _persist_and_dispatch_comment(
            tracking_id=tid,
            motivo="CLIENTE RECHAZA",
            comentario=f"Cliente pidió reagendar a {hh:02d}:{mm:02d} via WhatsApp",
            user_id=identity.get("user_id"),
            user_display_name=f"cliente {sess.phone}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa-agent] reagenda falló: {e}")
    Session.delete(sess.phone)
    return f"✅ Anotado, vamos a coordinar para las {hh:02d}:{mm:02d}. Te avisamos antes."


def _on_awaiting_client_comment(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    tid = sess.context.get("tracking_id")
    if not text:
        return "Decime algo o 'menu' para volver."
    try:
        from comments import _persist_and_dispatch_comment
        _persist_and_dispatch_comment(
            tracking_id=tid,
            motivo="CLIENTE RECHAZA",
            comentario=f"Mensaje del cliente: {text[:200]}",
            user_id=identity.get("user_id"),
            user_display_name=f"cliente {sess.phone}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa-agent] comment cliente falló: {e}")
    Session.delete(sess.phone)
    return "✅ Mensaje enviado al conductor. Gracias."


_STATE_HANDLERS = {
    "awaiting_role": _on_awaiting_role,
    "awaiting_driver_id": _on_awaiting_driver_id,
    "menu_driver": _on_menu_driver,
    "awaiting_tracking": _on_awaiting_tracking,
    "choosing_motivo": _on_choosing_motivo,
    "awaiting_comentario": _on_awaiting_comentario,
    "done_motivo": _on_done_motivo,
    "awaiting_client_tracking": _on_awaiting_client_tracking,
    "menu_cliente": _on_menu_cliente,
    "awaiting_reagenda": _on_awaiting_reagenda,
    "awaiting_client_comment": _on_awaiting_client_comment,
}

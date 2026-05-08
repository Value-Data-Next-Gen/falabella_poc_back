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
            "FROM fpoc_drivers WHERE phone_e164 = ? AND active = 1",
            (phone,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {"driver_id": r[0], "name": r[1], "vehicle_id": int(r[2]) if r[2] else None,
            "vehicle_name": r[3], "phone_e164": r[4]}


def _find_persona_by_phone(phone: str) -> Optional[dict]:
    """Busca el número en cascada: drivers → users → contactos.

    Devuelve dict con `kind` que orienta el flujo del agente:
      - 'driver'  → conductor (tiene ruta asignada)
      - 'manager' → transport_manager / falabella_admin / falabella_ops
      - 'contact' → contacto de empresa (sin login)
    """
    drv = _find_driver_by_phone(phone)
    if drv is not None:
        return {
            "kind": "driver",
            "id": drv["driver_id"],
            "name": drv["name"],
            "vehicle_id": drv["vehicle_id"],
            "vehicle_name": drv["vehicle_name"],
            "empresa_id": None,
            "empresa_nombre": None,
        }
    # Para WhatsApp NO filtramos por activo (el flag es para login web).
    # Si la persona está cargada con su rol, la detectamos.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT u.user_id, u.email, u.display_name, u.role, u.empresa_id,
                   e.nombre AS empresa_nombre
            FROM fpoc_users u
            LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = u.empresa_id
            WHERE u.phone_e164 = ?
            """,
            (phone,),
        )
        r = cur.fetchone()
    if r is not None:
        return {
            "kind": "manager",
            "id": int(r[0]),
            "user_id": int(r[0]),
            "email": str(r[1]),
            "name": str(r[2]),
            "role": str(r[3]),
            "empresa_id": int(r[4]) if r[4] is not None else None,
            "empresa_nombre": str(r[5]) if r[5] else None,
            "is_falabella": str(r[3]) in ("falabella_admin", "falabella_ops"),
        }
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT c.contact_id, c.nombre, c.rol, c.empresa_id, e.nombre AS empresa_nombre
            FROM fpoc_empresa_contactos c
            LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = c.empresa_id
            WHERE c.phone_e164 = ? AND c.active = 1
            """,
            (phone,),
        )
        r = cur.fetchone()
    if r is not None:
        return {
            "kind": "contact",
            "id": int(r[0]),
            "contact_id": int(r[0]),
            "name": str(r[1]),
            "role": str(r[2]) if r[2] else "otro",
            "empresa_id": int(r[3]) if r[3] is not None else None,
            "empresa_nombre": str(r[4]) if r[4] else None,
        }
    return None


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
            ")",
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
    """Busca primero en el snapshot sintético (con p_fallo y ETA simulados);
    si no encuentra, cae a fpoc_simpli_visits (BD real, ids numéricos del Excel).
    Esto le permite al driver consultar tracking_ids que vienen de la importación
    real (no solo TRK*)."""
    from state import STATE
    if STATE.snapshot_df is not None:
        df = STATE.snapshot_df[STATE.snapshot_df["tracking_id"] == tracking_id]
        if not df.empty:
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
    # Fallback BD real (ids numéricos)
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT id, title, address, comuna, region,
                       Patente_falsa, Empresa_falsa, Drivername, status,
                       current_eta_cl, ruta_id
                FROM fpoc_simpli_visits
                WHERE CAST(id AS TEXT) = ? OR ruta_id = ?
                """,
                (tracking_id, tracking_id),
            )
            r = cur.fetchone()
        if r is None:
            return None
        eta_str = str(r[9]) if r[9] else ""
        # Extraer HH:MM del timestamp si tiene formato datetime
        eta_short = eta_str.split(" ")[1][:5] if " " in eta_str else eta_str[:5]
        return {
            "tracking_id": str(r[0]),
            "title": str(r[1] or ""),
            "address": str(r[2] or ""),
            "comuna": str(r[3] or ""),
            "vehicle_id": int(r[5]) if r[5] is not None else 0,
            "vehicle_name": f"PAT-{r[5]}" if r[5] is not None else "",
            "status": str(r[8] or "pending"),
            "window_end": "23:59",  # BD real no tiene window_end discreto; placeholder
            "eta": eta_short or "—",
            "p_fallo": 0.0,  # no hay predicción ML para BD real (modelo entrena sobre sintéticos)
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa-agent] _visit_by_tracking BD fallback falló: {e}")
        return None


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
def _vehicle_ids_for_empresa(empresa_id: Optional[int]) -> list[int]:
    """Vehicles asignados a una empresa via STATE.vehicle_empresa_map."""
    from state import STATE
    if empresa_id is None:
        return list(STATE.vehicle_empresa_map.keys())
    return [vid for vid, eid in STATE.vehicle_empresa_map.items() if eid == empresa_id]


def _empresa_kpis(empresa_id: Optional[int], is_falabella: bool) -> dict:
    """Visitas/alertas del snapshot, scopeadas a la empresa (si manager) o
    todas (si falabella_*)."""
    from state import STATE
    if STATE.snapshot_df is None:
        return {}
    df = STATE.snapshot_df
    if not is_falabella and empresa_id is not None:
        allowed = set(_vehicle_ids_for_empresa(empresa_id))
        df = df[df["vehicle_id"].isin(allowed)]
    return {
        "total": int(len(df)),
        "pending": int((df["status"] == "pending").sum()),
        "completed": int((df["status"] == "completed").sum()),
        "alerts": int(df["alert_valuedata"].sum()),
        "alerts_critical": int(((df["alert_valuedata"]) & (df["p_fallo"] >= 0.7)).sum()),
    }


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


def _render_manager_menu(persona: dict) -> str:
    """Menú para transport_manager / falabella_admin / falabella_ops."""
    name = persona["name"]
    empresa_str = (
        f" · {persona['empresa_nombre']}" if persona.get("empresa_nombre")
        else " · vista global Falabella"
    )
    role_label = "Falabella" if persona.get("is_falabella") else "Manager"
    return (
        f"Hola {name} 👋  ({role_label}{empresa_str})\n"
        "¿Qué querés ver?\n"
        " 1️⃣  KPIs de hoy\n"
        " 2️⃣  Alertas críticas\n"
        " 3️⃣  Listar mis drivers\n"
        " 4️⃣  Buscar visita (TRK)\n"
        " 5️⃣  Reportar incidente\n"
        " 9️⃣  Salir"
    )


def _render_manager_kpis(persona: dict) -> str:
    is_falabella = bool(persona.get("is_falabella"))
    k = _empresa_kpis(persona.get("empresa_id"), is_falabella)
    if not k:
        return "Backend warming up, probá de nuevo en unos segundos."
    head = "📊 KPIs hoy"
    if persona.get("empresa_nombre"):
        head += f" — {persona['empresa_nombre']}"
    return (
        f"{head}\n"
        f"• Visitas: {k['total']}\n"
        f"• Pendientes: {k['pending']}\n"
        f"• Completadas: {k['completed']}\n"
        f"• Alertas anticipadas: {k['alerts']}\n"
        f"• Alertas críticas (≥70%): {k['alerts_critical']}\n\n"
        "Mandá '2' para ver las alertas, o 'menu' para volver."
    )


def _render_manager_alerts(persona: dict, limit: int = 5) -> str:
    from state import STATE
    if STATE.snapshot_df is None:
        return "Snapshot no listo."
    df = STATE.snapshot_df
    is_falabella = bool(persona.get("is_falabella"))
    if not is_falabella and persona.get("empresa_id") is not None:
        allowed = set(_vehicle_ids_for_empresa(persona["empresa_id"]))
        df = df[df["vehicle_id"].isin(allowed)]
    alerts = df[df["alert_valuedata"]].sort_values("p_fallo", ascending=False).head(limit)
    if alerts.empty:
        return "✅ Sin alertas anticipadas en este momento."
    lines = [f"⚠️ Top {len(alerts)} alertas (de mayor riesgo):"]
    for _, r in alerts.iterrows():
        risk = int(float(r["p_fallo"]) * 100)
        lines.append(
            f"  · {r['tracking_id']} {risk}% — {str(r['title'])[:25]} ({str(r.get('comuna', ''))[:15]}) {str(r['vehicle_name'])}"
        )
    lines.append("\nPara ver detalle: '4' y pegá el TRK. 'menu' para volver.")
    return "\n".join(lines)


def _render_manager_drivers(persona: dict) -> str:
    """Lista los drivers de la empresa con métricas básicas."""
    from state import STATE
    if STATE.snapshot_df is None:
        return "Snapshot no listo."
    is_falabella = bool(persona.get("is_falabella"))
    if is_falabella:
        vehicles = list(STATE.vehicle_empresa_map.keys())
    else:
        vehicles = _vehicle_ids_for_empresa(persona.get("empresa_id"))
    if not vehicles:
        return "No hay vehículos asignados a tu empresa."
    df = STATE.snapshot_df[STATE.snapshot_df["vehicle_id"].isin(vehicles)]
    lines = ["🚚 Drivers / Vehículos:"]
    for vid in sorted(vehicles):
        sub = df[df["vehicle_id"] == vid]
        if sub.empty:
            continue
        total = len(sub)
        pending = int((sub["status"] == "pending").sum())
        alerts = int(sub["alert_valuedata"].sum())
        # Resolver nombre del driver
        driver_name = "—"
        for d in STATE.drivers:
            if int(d.get("vehicle_id") or -1) == vid:
                driver_name = d.get("name", "—")
                break
        v_name = str(sub.iloc[0]["vehicle_name"])
        lines.append(f"  · {v_name}: {driver_name[:20]} — {total}vis ({pending}pend, {alerts}🔴)")
    if len(lines) == 1:
        return "Sin drivers activos en el snapshot."
    lines.append("\n'menu' para volver.")
    return "\n".join(lines)


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

    # 'salir' siempre limpia
    if text_lower in ("salir", "exit", "bye", "chao"):
        Session.delete(phone)
        return "👋 Listo, conversación cerrada. Mandá 'hola' para empezar de nuevo."

    # 'menu'/'hola'/'inicio'/'volver'/'start' resetea a idle y vuelve a entrar al
    # flujo (que correrá la auto-detección por phone).
    if text_lower in ("menu", "menú", "inicio", "volver", "start", "hola"):
        Session.delete(phone)
        # cae al cargar Session abajo, que vendrá idle y disparará auto-detect

    sess = Session.load(phone)

    # Si no hay sesión activa o el usuario es nuevo: arrancamos en awaiting_role
    if sess.state == "idle":
        # Auto-detect persona por phone → directo al menú correspondiente
        persona = _find_persona_by_phone(phone)
        if persona:
            kind = persona["kind"]
            if kind == "driver":
                sess.state = "menu_driver"
                sess.role = "driver"
                sess.identified_id = persona["id"]
                sess.context = {"driver": {
                    "driver_id": persona["id"], "name": persona["name"],
                    "vehicle_id": persona["vehicle_id"], "vehicle_name": persona["vehicle_name"],
                }}
                sess.save()
                return _render_driver_menu(sess.context["driver"]) + "\n\n(detecté tu número en el sistema)"
            if kind == "manager":
                sess.state = "menu_manager"
                sess.role = "manager"
                sess.identified_id = str(persona["id"])
                sess.context = {"persona": persona}
                sess.save()
                return _render_manager_menu(persona) + "\n\n(detecté tu número en el sistema)"
            if kind == "contact":
                rol = (persona.get("role") or "").lower()
                empresa_id = persona.get("empresa_id")

                # Jefe / coordinador → menú manager scopeado a su empresa.
                if rol in ("jefe", "coordinador"):
                    mgr_persona = {
                        "name": persona["name"],
                        "empresa_id": empresa_id,
                        "empresa_nombre": persona.get("empresa_nombre"),
                        "is_falabella": False,
                    }
                    sess.state = "menu_manager"
                    sess.role = "manager"
                    sess.identified_id = str(persona["id"])
                    sess.context = {"persona": mgr_persona}
                    sess.save()
                    return (
                        _render_manager_menu(mgr_persona)
                        + "\n\n(detecté tu número como jefe en el sistema)"
                    )

                # Driver (contacto con rol=driver) → auto-asignar vehículo de su
                # empresa con menos visitas pendientes (load-balanced).
                if rol == "driver":
                    from state import STATE
                    veh_emp_map = STATE.vehicle_empresa_map or {}
                    vehicles_empresa = [vid for vid, eid in veh_emp_map.items() if eid == empresa_id]
                    if vehicles_empresa and STATE.snapshot_df is not None:
                        df = STATE.snapshot_df
                        counts = {
                            vid: int(((df["vehicle_id"] == vid) & (df["status"] == "pending")).sum())
                            for vid in vehicles_empresa
                        }
                        # Vehículo con menos visitas pendientes (balanceo simple).
                        vid = min(counts, key=lambda k: counts[k])
                        # Resolver vehicle_name desde el snapshot
                        sub = df[df["vehicle_id"] == vid]
                        vehicle_name = (
                            str(sub.iloc[0]["vehicle_name"]) if not sub.empty
                            else f"FAL-{1000 + vid - 1}"
                        )
                        driver_synth = {
                            "driver_id": f"CONTACT-{persona['id']}",
                            "name": persona["name"],
                            "vehicle_id": vid,
                            "vehicle_name": vehicle_name,
                        }
                        sess.state = "menu_driver"
                        sess.role = "driver"
                        sess.identified_id = driver_synth["driver_id"]
                        sess.context = {"driver": driver_synth}
                        sess.save()
                        return (
                            _render_driver_menu(driver_synth)
                            + f"\n\n(te asigné el {vehicle_name} de {persona.get('empresa_nombre','tu empresa')})"
                        )
                    # Fallback: empresa sin vehicles cargados → role menu
                    sess.state = "awaiting_role"
                    sess.role = "contact"
                    sess.identified_id = str(persona["id"])
                    sess.context = {"persona": persona}
                    sess.save()
                    return (
                        f"Hola {persona['name']} 👋\n"
                        f"No pude encontrar un vehículo asignado en {persona.get('empresa_nombre','tu empresa')}.\n"
                        + _render_role_menu(profile_name)
                    )

                # Otro rol o sin rol → role menu manual.
                sess.state = "awaiting_role"
                sess.role = "contact"
                sess.identified_id = str(persona["id"])
                sess.context = {"persona": persona}
                sess.save()
                return (
                    f"Hola {persona['name']} 👋\n"
                    f"Estás registrado como contacto de {persona.get('empresa_nombre','la empresa')}.\n"
                    + _render_role_menu(profile_name)
                )
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
    sess.state = "describing_incident"
    sess.context["tracking_id"] = visit["tracking_id"]
    sess.save()
    risk_pct = int(visit["p_fallo"] * 100)
    risk = "🟢" if risk_pct < 30 else ("🟡" if risk_pct < 50 else "🔴")
    return (
        f"Visita: {visit['title']}\n"
        f"  {visit['comuna']} — Window {visit['window_end'][:5]}\n"
        f"  Riesgo: {risk} {risk_pct}%\n\n"
        "🤖 Contame qué pasó (con tus palabras) y la IA detecta el motivo.\n"
        "Mandá '0' si preferís elegir de una lista, o 'menu' para cancelar."
    )


def _on_describing_incident(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    """Driver describe el incidente en lenguaje natural; IA clasifica."""
    if text == "0":
        # Fallback a menú numerado clásico
        tid = sess.context.get("tracking_id")
        visit = _visit_by_tracking(tid) if tid else None
        sess.state = "choosing_motivo"
        sess.save()
        return _render_motivo_menu(visit) if visit else "Algo salió mal, mandá 'menu'."
    if len(text) < 5:
        return "Necesito un poco más de detalle. Contame qué pasó (mín. 5 caracteres) o '0' para lista."

    tid = sess.context.get("tracking_id")
    if not tid:
        sess.reset()
        sess.save()
        return "Algo salió mal con el flujo, mandá 'menu'."

    # Empresa para resolver descripciones override por empresa
    empresa_id = None
    persona = sess.context.get("persona")
    if persona and persona.get("empresa_id"):
        empresa_id = int(persona["empresa_id"])
    elif sess.context.get("driver"):
        from state import STATE
        vid = sess.context["driver"].get("vehicle_id")
        if vid is not None:
            empresa_id = STATE.vehicle_empresa_map.get(int(vid))

    # Clasificar con LLM (cae a keywords si no hay creds)
    try:
        from motivo_classifier import _classify_llm, _classify_keywords
        result = _classify_llm(text, empresa_id) or _classify_keywords(text)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[wa-agent] classifier falló: {e}")
        result = {"motivo": "SIN MORADORES", "confianza": "baja",
                  "razonamiento": "Sin clasificador disponible", "fallback": True}

    sess.context["ia_motivo"] = result["motivo"]
    sess.context["ia_confianza"] = result["confianza"]
    sess.context["ia_razonamiento"] = result["razonamiento"]
    sess.context["ia_fallback"] = bool(result.get("fallback", False))
    sess.context["comentario_libre"] = text
    sess.state = "confirming_ia_motivo"
    sess.save()

    badge = "🤖" if not result.get("fallback") else "📚"
    return (
        f"{badge} Analizando…\n"
        f"Detecté: *{result['motivo']}* (confianza {result['confianza']})\n"
        f"Razón: {result['razonamiento']}\n\n"
        "¿Confirmás?\n"
        " 1️⃣  Sí, registrar\n"
        " 2️⃣  No, elegir otro motivo\n"
        " 0️⃣  Cancelar"
    )


def _on_confirming_ia_motivo(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    tid = sess.context.get("tracking_id")
    motivo = sess.context.get("ia_motivo")
    comentario = sess.context.get("comentario_libre", "")
    if not tid or not motivo:
        sess.reset()
        sess.save()
        return "Algo salió mal, mandá 'menu'."

    if text == "1" or text_lower in ("si", "sí", "confirmo", "ok"):
        try:
            from comments import _persist_and_dispatch_comment
            actor = sess.identified_id or sess.phone
            _persist_and_dispatch_comment(
                tracking_id=tid,
                motivo=motivo,
                comentario=comentario + f" [IA: {sess.context.get('ia_confianza','?')}]",
                user_id=identity.get("user_id"),
                user_display_name=str(actor),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wa-agent] persist IA falló: {e}")
            sess.state = "menu_driver"
            sess.save()
            return f"❌ No pude registrar: {e}\nMandá 'menu'."
        sess.state = "done_motivo"
        for k in ("ia_motivo", "ia_confianza", "ia_razonamiento", "ia_fallback", "comentario_libre"):
            sess.context.pop(k, None)
        sess.save()
        return _render_done_motivo(tid, motivo)

    if text == "2" or "no" in text_lower or "otro" in text_lower:
        # IA equivocada → registramos correction sugerida y mostramos menú clásico
        try:
            # Persistimos el rejection como hint para mejorar el modelo
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    """
                    INSERT INTO fpoc_motivo_corrections
                      (comment_id, tracking_id, motivo_reportado, motivo_sugerido,
                       confianza, razonamiento, driver_id, status, region)
                    VALUES (NULL, ?, ?, ?, ?, ?, ?, 'rejected', NULL)
                    """,
                    (tid, motivo, "PENDING_USER_CHOICE",
                     sess.context.get("ia_confianza", "?"),
                     f"Driver rechazó IA: {sess.context.get('ia_razonamiento','')}",
                     sess.identified_id or sess.phone),
                )
                cn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[wa-agent] correction-rejected log falló: {e}")
        # Conservamos comentario_libre para usar en el flujo manual
        sess.state = "choosing_motivo"
        sess.save()
        visit = _visit_by_tracking(tid)
        return (
            "OK, elegí el motivo correcto entonces. La IA aprende de esto 👇\n\n"
            + (_render_motivo_menu(visit) if visit else _render_role_menu(None))
        )

    if text == "0":
        sess.state = "menu_driver"
        for k in ("tracking_id", "ia_motivo", "ia_confianza", "ia_razonamiento", "comentario_libre"):
            sess.context.pop(k, None)
        sess.save()
        driver = sess.context.get("driver")
        return _render_driver_menu(driver) if driver else _render_role_menu(None)

    return "Elegí 1 (confirmar), 2 (cambiar motivo) o 0 (cancelar)."


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
    sess.context["motivo"] = motivo
    # Si veníamos del flujo IA con un comentario libre ya capturado, lo
    # registramos junto al motivo elegido manualmente (la IA aprende de esto
    # via fpoc_motivo_corrections).
    if sess.context.get("comentario_libre"):
        prev = sess.context.get("comentario_libre", "")
        ia_motivo = sess.context.get("ia_motivo")
        try:
            from comments import _persist_and_dispatch_comment
            from db import get_conn as _gc
            actor = sess.identified_id or sess.phone
            _persist_and_dispatch_comment(
                tracking_id=sess.context["tracking_id"],
                motivo=motivo,
                comentario=prev + " [IA sugería " + str(ia_motivo) + ", driver corrigió]",
                user_id=identity.get("user_id"),
                user_display_name=str(actor),
            )
            # Update correction status: rechazado pero motivo final conocido
            with _gc() as cn:
                cn.execute(
                    """
                    UPDATE fpoc_motivo_corrections SET motivo_sugerido = ?, status = 'corrected'
                    WHERE tracking_id = ? AND status = 'rejected'
                      AND motivo_sugerido = 'PENDING_USER_CHOICE'
                    """,
                    (motivo, sess.context["tracking_id"]),
                )
                cn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wa-agent] persist via IA-correction falló: {e}")
            sess.state = "menu_driver"
            sess.save()
            return f"❌ Error al registrar: {e}\nMandá 'menu'."
        # limpiar contexto IA y cerrar
        for k in ("ia_motivo", "ia_confianza", "ia_razonamiento", "ia_fallback",
                  "comentario_libre", "motivo"):
            sess.context.pop(k, None)
        sess.state = "done_motivo"
        sess.save()
        return _render_done_motivo(sess.context.get("tracking_id", "?"), motivo)
    sess.state = "awaiting_comentario"
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


def _on_menu_manager(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    persona = sess.context.get("persona")
    if not persona:
        sess.reset()
        sess.save()
        return _render_role_menu(None)
    if text == "1" or "kpi" in text_lower:
        return _render_manager_kpis(persona) + "\n"
    if text == "2" or "alert" in text_lower:
        return _render_manager_alerts(persona) + "\n"
    if text == "3" or "driver" in text_lower or "conductor" in text_lower:
        return _render_manager_drivers(persona) + "\n"
    if text == "4" or text_lower.startswith("trk"):
        # Si tipearon TRK directamente, atajo
        if text_lower.startswith("trk"):
            visit = _visit_by_tracking(text.upper().strip())
            if visit:
                return _render_client_visit(visit) + "\n\n('menu' para volver)"
            return f"No encuentro {text}. Probá otro tracking_id o 'menu'."
        sess.state = "manager_search_tracking"
        sess.save()
        return "Pegá el tracking_id que querés inspeccionar (ej TRK0600009) o '0' para cancelar:"
    if text == "5" or "reportar" in text_lower or "incidente" in text_lower:
        # Manager también puede reportar como driver. Reusamos awaiting_tracking.
        sess.state = "awaiting_tracking"
        # Cargamos un "driver virtual" con el manager para que persistencia funcione
        sess.context["driver"] = {
            "driver_id": f"MGR-{persona['id']}",
            "name": persona["name"],
            "vehicle_id": None,
            "vehicle_name": persona.get("empresa_nombre", "manager"),
        }
        sess.save()
        return "Decime el tracking_id (ej: TRK0600009) o '0' para cancelar:"
    if text == "9" or text_lower in ("salir", "exit"):
        Session.delete(sess.phone)
        return "👋 Hasta luego."
    return "No entendí. Elegí 1, 2, 3, 4, 5 o 9. 'menu' para volver."


def _on_manager_search_tracking(sess: Session, text: str, text_lower: str, identity: dict) -> str:
    if text == "0":
        persona = sess.context.get("persona")
        sess.state = "menu_manager"
        sess.save()
        return _render_manager_menu(persona) if persona else _render_role_menu(None)
    visit = _visit_by_tracking(text.upper().strip())
    if visit is None:
        return f"No encuentro {text}. Pegá el tracking_id completo (ej TRK0600009) o '0' para cancelar."
    sess.state = "menu_manager"
    sess.save()
    return _render_client_visit(visit) + "\n\nMandá '2' para alertas o 'menu' para el menú."


_STATE_HANDLERS = {
    "awaiting_role": _on_awaiting_role,
    "awaiting_driver_id": _on_awaiting_driver_id,
    "menu_driver": _on_menu_driver,
    "menu_manager": _on_menu_manager,
    "manager_search_tracking": _on_manager_search_tracking,
    "awaiting_tracking": _on_awaiting_tracking,
    "describing_incident": _on_describing_incident,
    "confirming_ia_motivo": _on_confirming_ia_motivo,
    "choosing_motivo": _on_choosing_motivo,
    "awaiting_comentario": _on_awaiting_comentario,
    "done_motivo": _on_done_motivo,
    "awaiting_client_tracking": _on_awaiting_client_tracking,
    "menu_cliente": _on_menu_cliente,
    "awaiting_reagenda": _on_awaiting_reagenda,
    "awaiting_client_comment": _on_awaiting_client_comment,
}

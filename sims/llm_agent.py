"""Agente conversacional WhatsApp HÍBRIDO con Azure OpenAI gpt-4o-mini.

Cuando el FSM regex no matcheó nada y la sesión está idle, este módulo toma el
mensaje del usuario, se lo manda al LLM con tools enabled, ejecuta la tool que
el modelo elige y devuelve una respuesta en lenguaje natural.

Compliance/quick commands (ACTIVAR, stop, help, info, humano, gracias) **NO**
pasan por acá — los maneja `_dispatch` en `routers/twilio_inbound.py` ANTES de
llamar al agente. Acá solo llegan los textos libres tipo "¿cómo va mi ruta?"
o "necesito reagendar el TRK0123 a las 4".

Diseño:
  - 1 system prompt con identidad, day_state, catálogo de motivos, tono.
  - 7 tools que envuelven helpers ya existentes en twilio_inbound + whatsapp_agent.
  - Loop: chat → si tool_call → ejecutar → re-chat con el tool result → reply.
  - Máx 2 rounds de tool-calling. Timeout total 10s (Twilio webhook tope 15s).
  - Si OpenAI falla → caller hace fallback al FSM legacy.

NO usa SDK aparte. Reusa `openai.AzureOpenAI` ya presente en motivo_classifier.
"""
from __future__ import annotations

import json as _json
import os
import time
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from core.db import get_conn


def _mask_phone(phone: Optional[str]) -> str:
    """Masking de phone para logs (CR fixes-qa L8): '+56...XXXX'."""
    if not phone:
        return "—"
    p = str(phone)
    if len(p) <= 4:
        return "***"
    return f"{p[:3]}...{p[-4:]}"


# =============================================================================
# Identity helpers — resuelven nombre/rol/empresa de cualquier número conocido
# =============================================================================
def _lookup_user_name(user_id: Optional[int]) -> Optional[str]:
    """Devuelve display_name del user_id (fpoc.users) o None."""
    if not user_id:
        return None
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT display_name FROM fpoc.users WHERE user_id = ?",
                (int(user_id),),
            )
            r = cur.fetchone()
        if r is None:
            return None
        return str(r[0]) if r[0] is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] _lookup_user_name({user_id}) falló: {e}")
        return None


def _lookup_driver(driver_id: Optional[str]) -> tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    """Devuelve (name, empresa_id, vehicle_id, vehicle_name) del driver o tupla de Nones."""
    if not driver_id:
        return (None, None, None, None)
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT name, empresa_id, vehicle_id, vehicle_name "
                "FROM fpoc.drivers WHERE driver_id = ? AND active = 1",
                (str(driver_id),),
            )
            r = cur.fetchone()
        if r is None:
            return (None, None, None, None)
        name = str(r[0]) if r[0] is not None else None
        empresa_id = int(r[1]) if r[1] is not None else None
        vehicle_id = int(r[2]) if r[2] is not None else None
        vehicle_name = str(r[3]) if r[3] is not None else None
        return (name, empresa_id, vehicle_id, vehicle_name)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] _lookup_driver({driver_id}) falló: {e}")
        return (None, None, None, None)


def _lookup_contact(contact_id: Optional[int]) -> tuple[Optional[str], Optional[int]]:
    """Devuelve (nombre, empresa_id) del contacto o tupla de Nones."""
    if not contact_id:
        return (None, None)
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT nombre, empresa_id FROM fpoc.empresa_contactos "
                "WHERE contact_id = ? AND active = 1",
                (int(contact_id),),
            )
            r = cur.fetchone()
        if r is None:
            return (None, None)
        name = str(r[0]) if r[0] is not None else None
        empresa_id = int(r[1]) if r[1] is not None else None
        return (name, empresa_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] _lookup_contact({contact_id}) falló: {e}")
        return (None, None)


def _lookup_empresa_nombre(empresa_id: Optional[int]) -> Optional[str]:
    if not empresa_id:
        return None
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT nombre FROM fpoc.empresas_transporte WHERE empresa_id = ?",
                (int(empresa_id),),
            )
            r = cur.fetchone()
        if r is None:
            return None
        return str(r[0]) if r[0] is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] _lookup_empresa_nombre({empresa_id}) falló: {e}")
        return None


def _summarize_identity(identity: dict) -> dict:
    """Resume la identidad cruda de `_identify_phone` en un dict canónico que
    se inyecta al system prompt y a los tool handlers.

    Prioridad: user (rol explicito) > driver > contacto > anónimo.
    Un mismo número puede estar simultáneamente en las 3 tablas (ej Gonzalo:
    DRV-003 + contacto 5 + posiblemente user). Tomamos user primero porque su
    `role` es lo más informativo para el LLM.
    """
    identity = identity or {}
    user_role = identity.get("user_role")  # transport_manager / falabella_admin / falabella_ops
    user_id = identity.get("user_id")
    driver_id = identity.get("driver_id")
    contact_id = identity.get("contact_id")

    name: Optional[str] = None
    role_label: str
    empresa_id: Optional[int] = None
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None

    # Si es driver: SIEMPRE resolvemos vehicle/empresa desde fpoc.drivers para
    # tener los datos completos disponibles a las tools (get_route).
    if driver_id:
        d_name, d_empresa, vehicle_id, vehicle_name = _lookup_driver(driver_id)
        if d_name:
            name = d_name
        if d_empresa is not None:
            empresa_id = d_empresa

    if user_role:
        role_label = str(user_role)
        u_name = _lookup_user_name(user_id)
        if u_name:
            name = u_name
        if identity.get("user_empresa_id") is not None:
            empresa_id = identity.get("user_empresa_id")
    elif driver_id:
        role_label = "driver"
    elif contact_id:
        role_label = "contacto"
        c_name, c_empresa = _lookup_contact(contact_id)
        if c_name:
            name = c_name
        if c_empresa is not None and empresa_id is None:
            empresa_id = c_empresa
    else:
        role_label = "anonimo"

    empresa_nombre = _lookup_empresa_nombre(empresa_id)

    summary = {
        "name": name or "amigo",
        "role": role_label,
        "is_admin_or_ops": role_label in ("falabella_admin", "falabella_ops"),
        "empresa_id": empresa_id,
        "empresa_nombre": empresa_nombre,
        "driver_id": driver_id,
        "contact_id": contact_id,
        "user_id": user_id,
        "vehicle_id": vehicle_id,
        "vehicle_name": vehicle_name,
    }
    return summary


# =============================================================================
# Configuración Azure OpenAI (mismo patrón que routers/motivo_classifier.py)
# =============================================================================
def _azure_creds() -> Optional[dict]:
    endpoint = (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("AZURE_ENDPOINT")
        or ""
    ).strip().strip('"').strip("'")
    api_key = (
        os.environ.get("AZURE_OPENAI_API_KEY")
        or os.environ.get("AZURE_API_KEY")
        or ""
    ).strip().strip('"').strip("'")
    deployment = (
        os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or os.environ.get("AZURE_CHAT_DEPLOYMENT")
        or "gpt-4o-mini"
    ).strip().strip('"').strip("'")
    api_version = (
        os.environ.get("AZURE_OPENAI_API_VERSION")
        or os.environ.get("AZURE_API_VERSION")
        or "2024-12-01-preview"
    ).strip().strip('"').strip("'")
    if not endpoint or not api_key:
        return None
    return {
        "endpoint": endpoint,
        "api_key": api_key,
        "deployment": deployment,
        "api_version": api_version,
    }


# =============================================================================
# Tools — schemas OpenAI function-calling
# =============================================================================
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_route",
            "description": (
                "Devuelve un resumen de la ruta del día del driver (total stops, "
                "pendientes, completadas, alertas en riesgo). Usar cuando el "
                "usuario pregunta '¿cómo va mi ruta?', 'mi ruta de hoy', "
                "'cuántas entregas me quedan', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "driver_id": {
                        "type": "string",
                        "description": (
                            "Opcional. ID del driver (ej D-001). Si no se pasa, "
                            "se toma del identity del usuario."
                        ),
                    },
                    "ruta_id": {
                        "type": "string",
                        "description": (
                            "Opcional. ID de ruta tipo R-YYYYMMDD-NNN si el "
                            "usuario quiere consultar una ruta puntual (manager)."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_kpis",
            "description": (
                "Devuelve KPIs del día: total visitas, pendientes, completadas, "
                "alertas anticipadas. Usar cuando el usuario pregunta 'KPIs', "
                "'resumen del día', 'cómo va el día', '¿cuántas alertas hay?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": (
                "Devuelve estado, ETA y riesgo de una visita por tracking_id. "
                "Usar cuando el usuario pregunta por una visita específica y "
                "tiene un tracking_id tipo TRK..."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tracking_id": {
                        "type": "string",
                        "description": "ID de seguimiento (ej TRK0600009).",
                    },
                },
                "required": ["tracking_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_folio",
            "description": (
                "Busca un folio (reference) en visitas. Acepta 'FAL-1234' o "
                "número directo. Devuelve cliente, dirección, driver, ETA, "
                "subfolios."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folio": {
                        "type": "string",
                        "description": "Folio o reference (ej FAL-1234, 14246784).",
                    },
                },
                "required": ["folio"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_motivo",
            "description": (
                "Registra el motivo de no-entrega de una visita. SOLO para "
                "drivers o contactos con visitas asignadas. El motivo debe ser "
                "uno del catálogo (SIN MORADORES, CLIENTE RECHAZA, etc). El "
                "comentario es texto libre con detalle de qué pasó."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tracking_id": {
                        "type": "string",
                        "description": "ID de seguimiento de la visita.",
                    },
                    "motivo": {
                        "type": "string",
                        "description": "Motivo del catálogo canónico.",
                    },
                    "comentario": {
                        "type": "string",
                        "description": "Detalle de qué pasó (libre).",
                    },
                },
                "required": ["tracking_id", "motivo", "comentario"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reagendar",
            "description": (
                "Reagenda una visita a una hora específica (HH:MM, 24h). "
                "Registra un comment con motivo 'CLIENTE RECHAZA' y la nueva "
                "ventana solicitada."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tracking_id": {
                        "type": "string",
                        "description": "ID de seguimiento.",
                    },
                    "hh_mm": {
                        "type": "string",
                        "description": "Hora destino formato HH:MM (24h).",
                    },
                },
                "required": ["tracking_id", "hh_mm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Escala la conversación a un operador humano. Usar SI el "
                "usuario pide 'hablar con alguien', 'un humano', 'un "
                "coordinador', o si el LLM detecta una situación de urgencia "
                "que excede las tools disponibles."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# =============================================================================
# Tool handlers (envuelven helpers existentes; reciben kwargs del LLM + ctx)
# =============================================================================
def _tool_get_route(args: dict, ctx: dict) -> str:
    # Si pasaron ruta_id explícito (manager), usar _cmd_ruta.
    ruta_id = (args.get("ruta_id") or "").strip()
    if ruta_id:
        try:
            from routers.twilio_inbound import _cmd_ruta
            return _cmd_ruta(ruta_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[llm_agent] _cmd_ruta falló: {e}")
            return f"No pude obtener la ruta {ruta_id}."

    summary = ctx.get("summary") or {}
    arg_driver_id = (args.get("driver_id") or "").strip()
    # Prioridad: arg LLM explícito > driver_id del summary (caller es driver)
    driver_id: Optional[str] = arg_driver_id or summary.get("driver_id")

    if not driver_id:
        # No es driver y no pidió uno: si es admin/ops devolvemos KPIs como
        # mejor alternativa; si no, decimos que no identificamos su ruta.
        if summary.get("is_admin_or_ops"):
            return _tool_get_kpis({}, ctx)
        return (
            "No identifico tu ruta — debe haber un driver asociado a tu número. "
            "Si sos conductor, pedile al coordinador que vincule tu WhatsApp."
        )

    # Si el LLM mandó un driver_id distinto al del summary, lo resolvemos por DB.
    # Si coincide con el del summary, reutilizamos los datos ya cacheados (evita
    # ida y vuelta a DB y evita el bug de CAST(vehicle_id AS TEXT) que tiene
    # _find_driver_by_id_or_rut en whatsapp_agent).
    if arg_driver_id and arg_driver_id != (summary.get("driver_id") or ""):
        name, empresa_id, vehicle_id, vehicle_name = _lookup_driver(arg_driver_id)
        if name is None and empresa_id is None and vehicle_id is None:
            return f"No encuentro al driver {arg_driver_id}."
    else:
        name = summary.get("name")
        vehicle_id = summary.get("vehicle_id")
        vehicle_name = summary.get("vehicle_name")

    if vehicle_id is None:
        return (
            f"El driver {driver_id} no tiene vehículo asignado para hoy. "
            "Pedile al coordinador que asigne un vehículo."
        )

    # Construimos el dict que _render_route espera (mismo shape que arma el
    # FSM driver flow en whatsapp_agent._on_awaiting_driver_id).
    driver_dict = {
        "driver_id": str(driver_id),
        "name": name or "",
        "vehicle_id": int(vehicle_id),
        "vehicle_name": str(vehicle_name) if vehicle_name else f"PAT-{vehicle_id}",
    }
    try:
        from sims.whatsapp_agent import _render_route
        return _render_route(driver_dict)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] get_route falló: {e}")
        return "No pude leer tu ruta en este momento."


def _tool_get_kpis(args: dict, ctx: dict) -> str:
    try:
        from routers.twilio_inbound import _cmd_kpis
        return _cmd_kpis()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] get_kpis falló: {e}")
        return "No pude leer los KPIs en este momento."


def _tool_get_status(args: dict, ctx: dict) -> str:
    tid = (args.get("tracking_id") or "").strip()
    if not tid:
        return "Necesito un tracking_id para buscar la visita."
    try:
        from routers.twilio_inbound import _cmd_status
        return _cmd_status(tid)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] get_status falló: {e}")
        return f"No pude consultar {tid}."


def _tool_get_folio(args: dict, ctx: dict) -> str:
    folio = (args.get("folio") or "").strip()
    if not folio:
        return "Necesito el folio para buscarlo."
    try:
        from routers.twilio_inbound import _cmd_folio
        return _cmd_folio(folio)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] get_folio falló: {e}")
        return f"No pude leer el folio {folio}."


def _tool_report_motivo(args: dict, ctx: dict) -> str:
    tid = (args.get("tracking_id") or "").strip()
    motivo = (args.get("motivo") or "").strip()
    comentario = (args.get("comentario") or "").strip()
    if not tid or not motivo:
        return "Necesito tracking_id y motivo para registrar."
    identity = ctx.get("identity") or {}
    try:
        from routers.twilio_inbound import _cmd_motivo
        return _cmd_motivo(tid, motivo, comentario or "(sin detalle)", identity)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] report_motivo falló: {e}")
        return f"No pude registrar el motivo: {e}"


def _tool_reagendar(args: dict, ctx: dict) -> str:
    tid = (args.get("tracking_id") or "").strip()
    hh_mm = (args.get("hh_mm") or "").strip()
    if not tid or not hh_mm:
        return "Necesito tracking_id y hora (HH:MM) para reagendar."
    identity = ctx.get("identity") or {}
    try:
        from routers.twilio_inbound import _cmd_reagendar
        return _cmd_reagendar(tid, hh_mm, identity)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] reagendar falló: {e}")
        return f"No pude reagendar: {e}"


def _tool_escalate_to_human(args: dict, ctx: dict) -> str:
    phone = ctx.get("phone") or ""
    identity = ctx.get("identity") or {}
    try:
        from routers.twilio_inbound import _cmd_human
        return _cmd_human(phone, identity)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] escalate_to_human falló: {e}")
        return (
            "Te escalé a un operador. Si es urgente, llamá al call center."
        )


_TOOL_HANDLERS = {
    "get_route": _tool_get_route,
    "get_kpis": _tool_get_kpis,
    "get_status": _tool_get_status,
    "get_folio": _tool_get_folio,
    "report_motivo": _tool_report_motivo,
    "reagendar": _tool_reagendar,
    "escalate_to_human": _tool_escalate_to_human,
}


# =============================================================================
# System prompt
# =============================================================================
def _load_alert_context(phone: str) -> Optional[dict]:
    """Si la sesión WhatsApp del phone tiene un `last_alerted_tid` activo
    (timestamp `last_alerted_at` < 1h en UTC), devuelve `{tid, at}` para
    inyectarlo al system prompt. Si no, devuelve None.

    Soporta múltiples alertas activas: el campo `last_alerted_tid` se
    sobrescribe en cada nueva alerta (`POST /api/admin/notify-eta-breach`),
    por lo que siempre prevalece la MÁS RECIENTE. Eso evita ambigüedad
    cuando el driver responde con texto libre.
    """
    if not phone:
        return None
    try:
        from sims.whatsapp_agent import Session  # noqa: WPS433
        sess = Session.load(phone)
        ctx = sess.context or {}
        tid = ctx.get("last_alerted_tid")
        at = ctx.get("last_alerted_at")
        if not tid or not at:
            return None
        try:
            ts = datetime.fromisoformat(str(at).replace("Z", "+00:00").split("+")[0])
        except Exception:  # noqa: BLE001
            return None
        delta = datetime.utcnow() - ts
        if delta.total_seconds() > 3600:  # 1h TTL
            return None
        return {"tracking_id": str(tid), "at": str(at)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_agent] _load_alert_context({_mask_phone(phone)}) failed: {e}")
        return None


def _build_system_prompt(
    summary: dict,
    day_state: str,
    alert_context: Optional[dict] = None,
) -> str:
    role = str(summary.get("role") or "anonimo").lower()
    name = summary.get("name") or "amigo"
    empresa_nombre = summary.get("empresa_nombre") or "(empresa no identificada)"
    is_admin = bool(summary.get("is_admin_or_ops"))
    day_active = day_state in ("EN_CURSO", "PAUSADO")
    driver_id = summary.get("driver_id")
    extra_ids: list[str] = []
    if driver_id:
        extra_ids.append(f"driver_id={driver_id}")
    if summary.get("vehicle_name"):
        extra_ids.append(f"vehículo={summary['vehicle_name']}")
    if summary.get("contact_id"):
        extra_ids.append(f"contact_id={summary['contact_id']}")
    ids_clause = f" ({'; '.join(extra_ids)})" if extra_ids else ""

    try:
        from routers.comments import MOTIVOS_CATALOGO
        motivos = MOTIVOS_CATALOGO
    except Exception:  # noqa: BLE001
        motivos = []

    motivos_str = ", ".join(motivos) if motivos else "(catálogo no disponible)"

    # Si el driver recibió hace < 1h una alerta ETA (POST /api/admin/notify-eta-breach),
    # se inyecta el tracking_id en el system prompt para que el LLM lo use al
    # llamar `report_motivo` sin que el driver tenga que tipearlo.
    alert_clause = ""
    if alert_context and alert_context.get("tracking_id"):
        _alert_tid = alert_context["tracking_id"]
        alert_clause = (
            "\n\nCONTEXTO ACTIVO: El usuario recibió hace poco una alerta sobre "
            f"la visita TID:{_alert_tid}. Si el usuario responde con cualquier "
            "texto que parezca una causa/justificación (ej: 'se me pinchó la "
            "rueda', 'siniestro', 'no estaba en casa'), DEBES llamar la tool "
            f"`report_motivo` usando tracking_id={_alert_tid}, inferiendo el "
            "motivo del catálogo CATÁLOGO_MOTIVOS y poniendo el texto completo "
            "como comentario."
        )

    gate_clause = ""
    if not day_active and not is_admin:
        gate_clause = (
            "\n\nIMPORTANTE: El día operativo NO está iniciado (estado actual: "
            f"{day_state}). NO podés responder consultas operativas (ruta, "
            "KPIs, status de visitas) todavía. Si el usuario pregunta cosas "
            "operativas, decile amablemente que el día aún no arrancó y que "
            "puede escribir 'humano' para escalar a un coordinador o 'help' "
            "para ver comandos. SÍ podés responder saludos y meta-preguntas "
            "(qué hago, quién sos)."
        )

    return (
        f"Sos un asistente de Falabella ValueData (torre de control logística "
        f"de última milla en Chile). Hablás por WhatsApp con {name}, rol "
        f"'{role}' de la empresa '{empresa_nombre}'{ids_clause}. Estado del día "
        f"operativo: {day_state}.\n\n"
        "TONO Y FORMATO:\n"
        "- Cordial, breve, profesional. Español de Chile.\n"
        "- Cada respuesta < 280 caracteres si es posible.\n"
        "- Formato WhatsApp: sin markdown excesivo (sí emoji con moderación, "
        "sí *negrita* puntual, NO listas largas tipo '## Titulo').\n"
        "- No uses formato JSON ni código en las respuestas al usuario.\n\n"
        "TOOLS DISPONIBLES:\n"
        "Tenés tools para consultar y modificar el sistema. SIEMPRE preferí "
        "usar una tool antes que inventar datos. Si el usuario pide algo "
        "que NO podés resolver con las tools (ej: cambiar precios, hablar con "
        "alguien específico, problemas fuera del scope operativo), llamá "
        "`escalate_to_human` o decile que escriba 'humano'.\n\n"
        "CATÁLOGO DE MOTIVOS VÁLIDOS (para report_motivo):\n"
        f"{motivos_str}\n\n"
        "ATAJOS RÁPIDOS QUE EL USUARIO PUEDE ESCRIBIR DIRECTO (sin LLM):\n"
        "- 'help' / 'menu' → ayuda\n"
        "- 'stop' → baja\n"
        "- 'humano' → escalar a operador\n"
        "Si el usuario los menciona, recordáselo amablemente.\n\n"
        "REGLAS:\n"
        "1. Si el usuario te saluda o agradece, respondé corto y ofrecé "
        "ayuda (ej: '¿en qué te ayudo? ¿Querés ver tu ruta o KPIs?').\n"
        "2. Si pide algo operativo (ruta, status, KPIs, reagendar, reportar), "
        "llamá la tool correspondiente.\n"
        "3. Si el motivo que infieres no está en el catálogo, no llames "
        "report_motivo: pedile al usuario que aclare con palabras del catálogo "
        "o sugerí el más cercano.\n"
        "4. Errores típicos del usuario (typos, mayúsculas, falta de prefijo "
        "'TRK') son OK: usá la tool igual con tu mejor interpretación; si la "
        "tool devuelve error, decíselo amablemente.\n"
        "5. NO inventes tracking_ids, folios, ni datos. Si no tenés algo, "
        "preguntá."
        f"{gate_clause}"
        f"{alert_clause}"
    )


# =============================================================================
# Loop principal de chat con tool-calling
# =============================================================================
def _safe_parse_args(raw: str) -> dict:
    try:
        if not raw:
            return {}
        d = _json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def chat(message: str, identity: dict, day_state: str, phone: str = "") -> str:
    """Entrada pública del agente LLM.

    Args:
        message: texto del usuario (ya stripeado).
        identity: dict con info del usuario (role, display_name, empresa_id, etc).
        day_state: 'EN_CURSO' | 'BORRADOR' | 'VALIDADO' | 'CERRADO' | 'PAUSADO'.
        phone: número E.164 del usuario (para tools que necesitan phone, ej escalate).

    Returns:
        Texto plano de respuesta para mandar por WhatsApp.

    Raises:
        RuntimeError si no hay creds Azure (el caller hace fallback al FSM).
    """
    creds = _azure_creds()
    if creds is None:
        raise RuntimeError("Azure OpenAI sin credenciales configuradas")

    t0 = time.monotonic()
    summary = _summarize_identity(identity)
    ctx = {"identity": identity, "summary": summary, "phone": phone}
    logger.info(
        f"[llm_agent] phone={_mask_phone(phone)} role={summary.get('role')} "
        f"name={summary.get('name')} driver_id={summary.get('driver_id')} "
        f"empresa_id={summary.get('empresa_id')} day_state={day_state}"
    )

    try:
        from openai import AzureOpenAI
    except ImportError as e:
        raise RuntimeError(f"openai SDK no disponible: {e}") from e

    client = AzureOpenAI(
        azure_endpoint=creds["endpoint"],
        api_key=creds["api_key"],
        api_version=creds["api_version"],
    )
    deployment = creds["deployment"]

    alert_context = _load_alert_context(phone)
    system_prompt = _build_system_prompt(summary, day_state, alert_context)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    MAX_ROUNDS = 2
    TIMEOUT_S = 10.0

    tool_called: Optional[str] = None
    total_tokens = 0

    for round_idx in range(MAX_ROUNDS + 1):
        # Defensa-en-profundidad: si nos pasamos del presupuesto, cortar.
        elapsed = time.monotonic() - t0
        if elapsed >= TIMEOUT_S:
            logger.warning(
                f"[llm_agent] timeout interno tras {elapsed:.1f}s (round={round_idx})"
            )
            raise RuntimeError("LLM timeout")

        remaining = max(1.0, TIMEOUT_S - elapsed)
        try:
            resp = client.chat.completions.create(
                model=deployment,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=400,
                timeout=remaining,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[llm_agent] llamada OpenAI falló round={round_idx}: {e}")
            raise RuntimeError(f"OpenAI call failed: {e}") from e

        try:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
        except Exception:  # noqa: BLE001
            pass

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []

        # Caso A: el LLM devolvió texto final (no más tools).
        if not tool_calls:
            content = (getattr(msg, "content", None) or "").strip()
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                f"[llm_agent] phone={_mask_phone(phone)} role={summary.get('role')} "
                f"model={deployment} tokens={total_tokens} tool={tool_called or '-'} "
                f"latency_ms={latency_ms} len(reply)={len(content)}"
            )
            if not content:
                return (
                    "No pude armar una respuesta. Probá de nuevo o escribí "
                    "'humano' para hablar con un operador."
                )
            return content

        # Caso B: tool calls. Si superamos el máximo de rounds, forzar respuesta.
        if round_idx >= MAX_ROUNDS:
            logger.warning(
                f"[llm_agent] máx rounds {MAX_ROUNDS} alcanzado, forzando reply de texto"
            )
            # Pedimos una respuesta final sin tools.
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Llegamos al límite de tool calls. Respondé al usuario "
                        "con lo que ya sabés. No llames más tools."
                    ),
                }
            )
            continue

        # Append el message assistant con los tool_calls (requerido por API)
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        # Ejecutar cada tool y agregar el resultado al historial.
        for tc in tool_calls:
            fn_name = tc.function.name
            tool_called = fn_name  # último ejecutado, para logging
            args = _safe_parse_args(tc.function.arguments or "{}")
            handler = _TOOL_HANDLERS.get(fn_name)
            if handler is None:
                tool_result = (
                    f"Tool {fn_name} no existe. Solo: "
                    f"{', '.join(_TOOL_HANDLERS.keys())}"
                )
            else:
                try:
                    tool_result = handler(args, ctx)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[llm_agent] tool={fn_name} crash: {e}")
                    # Devolvemos el error como tool result así el LLM lo formatea
                    # en lenguaje natural en el siguiente round.
                    tool_result = (
                        "Hubo un problema procesando tu pedido. "
                        f"Detalle: {e}. Decile al usuario que pruebe de nuevo o "
                        "escriba 'humano'."
                    )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
            )

    # Si terminamos el loop sin return (no debería pasar), devolver algo.
    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.warning(
        f"[llm_agent] loop terminó sin reply phone={_mask_phone(phone)} "
        f"tokens={total_tokens} tool={tool_called} latency_ms={latency_ms}"
    )
    return (
        "Hubo un problema procesando tu pedido. Intentá de nuevo o escribí "
        "'humano' para hablar con un operador."
    )

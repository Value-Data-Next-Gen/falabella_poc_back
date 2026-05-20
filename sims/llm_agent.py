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
from typing import Any, Optional

from loguru import logger


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
    # Si no, intentamos resumir la ruta del driver del usuario.
    identity = ctx.get("identity") or {}
    driver_id = (args.get("driver_id") or "").strip() or identity.get("driver_id")
    if not driver_id:
        # Manager sin ruta_id explícita → devolver KPIs como mejor alternativa
        return _tool_get_kpis({}, ctx)
    try:
        from sims.whatsapp_agent import (
            _find_driver_by_id_or_rut,
            _render_route,
        )
        driver = _find_driver_by_id_or_rut(str(driver_id))
        if driver is None:
            return f"No encuentro al driver {driver_id}."
        return _render_route(driver)
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
def _build_system_prompt(identity: dict, day_state: str) -> str:
    role = str(identity.get("role") or identity.get("user_role") or "guest").lower()
    name = identity.get("display_name") or identity.get("name") or "amigo"
    empresa_nombre = identity.get("empresa_nombre") or "(empresa no identificada)"
    is_admin = role in ("falabella_admin", "falabella_ops")
    day_active = day_state in ("EN_CURSO", "PAUSADO")

    try:
        from routers.comments import MOTIVOS_CATALOGO
        motivos = MOTIVOS_CATALOGO
    except Exception:  # noqa: BLE001
        motivos = []

    motivos_str = ", ".join(motivos) if motivos else "(catálogo no disponible)"

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
        f"'{role}' de la empresa '{empresa_nombre}'. Estado del día operativo: "
        f"{day_state}.\n\n"
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
    ctx = {"identity": identity, "phone": phone}

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

    system_prompt = _build_system_prompt(identity, day_state)
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
                f"[llm_agent] phone={phone} role={identity.get('role') or identity.get('user_role')} "
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
        f"[llm_agent] loop terminó sin reply phone={phone} "
        f"tokens={total_tokens} tool={tool_called} latency_ms={latency_ms}"
    )
    return (
        "Hubo un problema procesando tu pedido. Intentá de nuevo o escribí "
        "'humano' para hablar con un operador."
    )

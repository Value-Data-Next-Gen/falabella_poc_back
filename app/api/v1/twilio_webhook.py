"""Twilio inbound webhook — handles WhatsApp messages.

POST /api/v1/twilio/webhook
  - ACTIVAR <token>: opt-in activation
  - Free text from activated users: routed to AI assistant
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_tools import actor_role, execute_tool, tool_definitions_for
from app.core.config import settings
from app.core.twilio_templates import cuenta_activada_sid
from app.core.whatsapp import send_whatsapp
from app.db.models.driver import Driver
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/twilio", tags=["twilio"])

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

_BOT_BASE = """Eres el bot de WhatsApp de Torre de Control (Falabella ultima milla).
Estas hablando con {nombre}.

Reglas:
- Responde en espanol chileno, breve y directo (maximo 300 caracteres por mensaje).
- Usa los tools para consultar datos reales; nunca inventes cifras.
- Si no entiendes, pide que repita. Solo puedes ayudar con lo correspondiente al rol del usuario.
"""

# Role-tailored guidance. Each actor only sees the tools its role allows
# (see ai_tools.tool_definitions_for), so the prompt matches the capabilities.
_ROLE_GUIDE = {
    "driver": """El usuario es un CONDUCTOR. Ayudalo SOLO con su operacion:
- Consultar un cliente por folio: usa SIEMPRE `obtener_info_cliente_por_folio`. Si `no_entregar` es true, AVISA PRIMERO y de forma TAJANTE: "⛔ NO ENTREGAR a este cliente. Motivo: <no_entregar_motivo>. No realices la entrega." Luego, si el cliente es VIP o tiene notas operativas, dilo PROMINENTEMENTE (ej: "Cliente VIP: razon X. Nota: Y").
- Reportar un motivo de no-entrega: usa `clasificar_motivo` con el catalogo oficial.
- Cancelar una visita por motivo legitimo: `cancelar_visita_manual`.
- Reportar un incidente en ruta (siniestro, demora grave): `crear_alerta_manual`.
No tienes acceso a datos administrativos ni de otros conductores.""",
    "contacto": """El usuario es un CONTACTO/JEFE de la empresa transportista. Ayudalo con la operacion de SU empresa unicamente:
alertas abiertas, resumen operativo, estado/lista de conductores, compliance de documentos, clasificar motivos, info de clientes por folio, y reportes (`obtener_reporte`).""",
    "manager": """El usuario es un TRANSPORT MANAGER. Ayudalo con la operacion de las empresas asignadas a el:
alertas, resumenes, conductores, compliance, clasificar motivos, info de clientes, y reportes (`obtener_reporte`).""",
    "falabella": """El usuario es de FALABELLA (admin/ops) con visibilidad de TODAS las empresas.
Ayudalo con KPIs, alertas, resumenes operativos, compliance, conductores, info de clientes y reportes (`obtener_reporte`) de cualquier empresa.""",
}


def _system_prompt(nombre: str, role: str) -> str:
    return _BOT_BASE.format(nombre=nombre) + "\n" + _ROLE_GUIDE.get(role, _ROLE_GUIDE["driver"])


def _twiml() -> Response:
    return Response(content=TWIML_EMPTY, media_type="application/xml")


async def _validate_signature(request: Request) -> None:
    """Verify the request really came from Twilio (HMAC over URL + POST params).

    Previously this only checked the header was PRESENT — a no-op that accepted
    forged requests. Now we validate the signature with the account auth token.
    Behind Azure App Service (TLS terminated at the proxy), `request.url` may
    show an internal host, so we sign against the configured public URL.
    """
    if not settings.twilio_inbound_validate_signature:
        return
    from twilio.request_validator import RequestValidator

    sig = request.headers.get("X-Twilio-Signature", "")
    token = settings.twilio_auth_token.get_secret_value()
    if not sig or not token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing Twilio signature")

    public = (settings.twilio_inbound_public_url or "").rstrip("/")
    url = f"{public}{request.url.path}" if public else str(request.url)
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if not RequestValidator(token).validate(url, params, sig):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid Twilio signature")


def _find_person_sync(driver: Driver | None, contacto: EmpresaContacto | None, user: User | None) -> tuple[str, str, str]:
    if driver:
        return driver.nombre, "conductor", driver.driver_id
    if contacto:
        return contacto.nombre, "contacto", str(contacto.contact_id)
    if user:
        return user.display_name, "usuario", str(user.user_id)
    return "Desconocido", "desconocido", ""


@router.post("/webhook", operation_id="twilioWebhook")
async def twilio_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _validate_signature(request)

    form = await request.form()
    from_number = str(form.get("From", "")).replace("whatsapp:", "")
    body = str(form.get("Body", "")).strip()

    logger.info(f"[twilio] inbound from={from_number} body={body[:100]}")

    # Explicit token activation (wa.me link path): "ACTIVAR <token>".
    if body.upper().startswith("ACTIVAR"):
        token = body.split(maxsplit=1)[1].strip() if " " in body else ""
        if token:
            await _handle_activation(db, from_number, token)
        else:
            await send_whatsapp(to=from_number, body="Para activarte responde: ACTIVAR seguido de tu codigo.")
        return _twiml()

    # Find who's messaging (by phone) — INCLUDING not-yet-activated invitees.
    driver = (await db.execute(select(Driver).where(Driver.phone_e164 == from_number))).scalar_one_or_none()
    contacto = (await db.execute(select(EmpresaContacto).where(EmpresaContacto.phone_e164 == from_number))).scalar_one_or_none() if not driver else None
    user = (await db.execute(select(User).where(User.phone_e164 == from_number))).scalar_one_or_none() if not driver and not contacto else None
    entity = driver or contacto or user

    # Opt-out (Meta requires honoring STOP). Only meaningful for a known number.
    if entity is not None and body.strip().upper() in ("STOP", "BAJA", "SALIR", "CANCELAR", "NO"):
        await _handle_optout(db, entity)
        await send_whatsapp(to=from_number, body="Listo, no recibiras mas mensajes. Responde ACTIVAR <codigo> para reactivar.")
        return _twiml()

    if entity is None:
        await send_whatsapp(to=from_number, body="No te tengo registrado. Pide a tu supervisor que te invite, o responde: ACTIVAR seguido de tu codigo.")
        return _twiml()

    # Reply-to-activate (hybrid onboarding): an invited person whose phone we
    # already have but who hasn't activated yet — any reply is consent. This
    # makes the INVITACION template ("responde para activarte") actually work.
    if not _is_activated(entity):
        await _activate_entity(db, entity, from_number)
        return _twiml()

    nombre, tipo, entity_id = _find_person_sync(driver, contacto, user)

    # The actor for ai_tools is whichever entity resolved by phone — drives
    # tenant scope for alerts tools (CR-022 Part B). Drivers and contactos are
    # bound to one empresa; users may be cross-empresa (falabella_*).
    actor: Driver | EmpresaContacto | User | None = driver or contacto or user
    reply = await _ai_reply(db, body, nombre, tipo, entity_id, actor=actor)
    await send_whatsapp(to=from_number, body=reply)

    return _twiml()


async def _ai_reply(
    db: AsyncSession,
    message: str,
    nombre: str,
    tipo: str,
    entity_id: str,
    actor: Driver | EmpresaContacto | User | None = None,
) -> str:
    if not settings.azure_openai_endpoint or not settings.azure_openai_api_key.get_secret_value():
        return "Sistema IA no disponible. Contacta a soporte."

    try:
        from openai import AsyncAzureOpenAI

        client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            api_version=settings.azure_openai_api_version,
        )

        system = _system_prompt(nombre, actor_role(actor))
        tools = tool_definitions_for(actor)

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]

        for _ in range(3):
            response = await client.chat.completions.create(
                model=settings.azure_openai_chat_deployment,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=300,
            )

            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                messages.append(choice.message.model_dump())
                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    fn_args = json.loads(tc.function.arguments)
                    logger.info(f"[whatsapp-bot] tool: {fn_name}({fn_args})")
                    result = await execute_tool(db, fn_name, fn_args, actor=actor)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue

            return choice.message.content or "No entendi. Escribe AYUDA para ver opciones."

        return "No pude procesar tu mensaje. Intenta de nuevo."
    except Exception as e:
        logger.error(f"[whatsapp-bot] AI error: {e}")
        return "Error del sistema. Intenta de nuevo en unos minutos."


def _entity_nombre(entity: Driver | EmpresaContacto | User) -> str:
    return entity.display_name if isinstance(entity, User) else entity.nombre


def _is_activated(entity: Driver | EmpresaContacto | User) -> bool:
    """Has this invitee already opted in? Drivers/contactos use opted_in_at;
    users use activation_used_at (they have no opt-in step)."""
    if isinstance(entity, Driver | EmpresaContacto):
        return entity.opted_in_at is not None
    return entity.activation_used_at is not None


async def _activate_entity(db: AsyncSession, entity: Driver | EmpresaContacto | User, phone: str) -> None:
    """Mark an invitee activated + opted-in and send the welcome template.
    Shared by the token path and the reply-to-activate (phone-match) path."""
    now = datetime.now(UTC)
    entity.phone_e164 = phone
    entity.activation_used_at = now
    if isinstance(entity, Driver | EmpresaContacto):
        entity.opted_in_at = now
    if hasattr(entity, "notify_whatsapp"):
        entity.notify_whatsapp = True
    await db.commit()
    nombre = _entity_nombre(entity) or ""
    kind = type(entity).__name__
    logger.info(f"[activation] {kind} activated phone={phone}")
    first = nombre.split()[0] if nombre.strip() else "👋"
    await send_whatsapp(to=phone, content_sid=cuenta_activada_sid(), content_variables={"1": first})


async def _handle_optout(db: AsyncSession, entity: Driver | EmpresaContacto | User) -> None:
    """Honor STOP/BAJA: stop notifying. Drivers/users have notify_whatsapp;
    for all we clear opted_in_at so they must re-activate to resume."""
    if hasattr(entity, "notify_whatsapp"):
        entity.notify_whatsapp = False
    if isinstance(entity, Driver | EmpresaContacto):
        entity.opted_in_at = None
    await db.commit()
    logger.info(f"[optout] {type(entity).__name__} opted out")


async def _handle_activation(db: AsyncSession, phone: str, token: str) -> None:
    """Token-based activation (wa.me link). Looks the token up across the three
    invitee types and activates the match."""
    for model in (Driver, EmpresaContacto, User):
        entity = (await db.execute(
            select(model).where(model.activation_token == token)
        )).scalar_one_or_none()
        if entity is not None:
            await _activate_entity(db, entity, phone)
            return
    logger.warning(f"[activation] token {token[:8]}... not found for phone {phone}")

    logger.warning(f"[activation] token {token[:8]}... not found for phone {phone}")


# ----------------------------------------------------------------------------
# Back-compat alias for the v1 / Twilio Console webhook path.
#
# Twilio is configured to POST inbound WhatsApp to `/api/twilio/inbound` (the v1
# path). v2's native route is `/api/v1/twilio/webhook`. To keep inbound working
# across the cutover WITHOUT touching the Twilio Console, we also answer the old
# path here and delegate to the same handler. A no-op `/status` absorbs Twilio
# delivery-status callbacks so they don't 404 / retry.
# ----------------------------------------------------------------------------
alias_router = APIRouter(prefix="/api/twilio", tags=["twilio"])


@alias_router.post("/inbound", operation_id="twilioInboundAlias")
async def twilio_inbound_alias(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    return await twilio_webhook(request, db)


@alias_router.post("/status", operation_id="twilioStatusCallback")
async def twilio_status_callback(request: Request) -> Response:
    """Absorb Twilio message-status callbacks (delivered/read/failed)."""
    form = await request.form()
    logger.info(
        f"[twilio] status sid={form.get('MessageSid')} status={form.get('MessageStatus')}"
    )
    return _twiml()

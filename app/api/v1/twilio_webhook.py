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

from app.core.ai_tools import TOOL_DEFINITIONS, execute_tool
from app.core.config import settings
from app.core.twilio_templates import cuenta_activada_sid
from app.core.whatsapp import send_whatsapp
from app.db.models.driver import Driver
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/twilio", tags=["twilio"])

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

BOT_SYSTEM_PROMPT = """Eres el bot de WhatsApp de Torre de Control (Falabella ultima milla).
Estas hablando con {nombre} ({tipo}).

Tu rol:
- Si es un conductor: ayudar con consultas de ruta, reportar motivos de no-entrega, verificar estado
- Si es un contacto/usuario: dar KPIs, alertas, estado de conductores, clasificar motivos

Reglas:
- Responde en espanol chileno, breve y directo (maximo 300 caracteres por mensaje)
- Usa los tools para consultar datos reales
- Para motivos de no-entrega, usa el catalogo oficial (tool clasificar_motivo)
- Si no entiendes, pide que repita
- Cuando un conductor o usuario pregunta sobre un folio cliente, un destinatario especifico, o una proxima entrega, llama SIEMPRE el tool `obtener_info_cliente_por_folio`. Si el cliente tiene `es_vip=true` o `notas_operativas` no vacias, mencionalo PROMINENTEMENTE en tu respuesta (ej: "Cliente VIP: razon X. Nota operativa: Y"). Esto es critico para que el conductor sepa como manejar la entrega.

Menu rapido (el usuario puede escribir el numero):
1. Estado operativo
2. Documentos pendientes
3. Clasificar motivo
4. Ayuda
"""


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

    if body.upper().startswith("ACTIVAR"):
        token = body.split(maxsplit=1)[1].strip() if " " in body else ""
        if token:
            await _handle_activation(db, from_number, token)
        return _twiml()

    # Find who's messaging
    driver = (await db.execute(select(Driver).where(Driver.phone_e164 == from_number))).scalar_one_or_none()
    contacto = (await db.execute(select(EmpresaContacto).where(EmpresaContacto.phone_e164 == from_number))).scalar_one_or_none() if not driver else None
    user = (await db.execute(select(User).where(User.phone_e164 == from_number))).scalar_one_or_none() if not driver and not contacto else None

    nombre, tipo, entity_id = _find_person_sync(driver, contacto, user)

    if tipo == "desconocido":
        await send_whatsapp(to=from_number, body="No te tengo registrado. Pide a tu supervisor que te invite a Torre de Control.")
        return _twiml()

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

        system = BOT_SYSTEM_PROMPT.format(nombre=nombre, tipo=tipo)

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]

        for _ in range(3):
            response = await client.chat.completions.create(
                model=settings.azure_openai_chat_deployment,
                messages=messages,
                tools=TOOL_DEFINITIONS,
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


async def _handle_activation(db: AsyncSession, phone: str, token: str) -> None:
    now = datetime.now(UTC)

    result = await db.execute(select(Driver).where(Driver.activation_token == token))
    driver = result.scalar_one_or_none()
    if driver:
        driver.phone_e164 = phone
        driver.opted_in_at = now
        driver.activation_used_at = now
        driver.notify_whatsapp = True
        await db.commit()
        logger.info(f"[activation] driver {driver.driver_id} activated")
        await send_whatsapp(to=phone, content_sid=cuenta_activada_sid(), content_variables={"1": driver.nombre.split()[0]})
        return

    result = await db.execute(select(EmpresaContacto).where(EmpresaContacto.activation_token == token))
    contacto = result.scalar_one_or_none()
    if contacto:
        contacto.phone_e164 = phone
        contacto.opted_in_at = now
        contacto.activation_used_at = now
        await db.commit()
        logger.info(f"[activation] contacto {contacto.contact_id} activated")
        await send_whatsapp(to=phone, content_sid=cuenta_activada_sid(), content_variables={"1": contacto.nombre.split()[0]})
        return

    result = await db.execute(select(User).where(User.activation_token == token))
    user = result.scalar_one_or_none()
    if user:
        user.phone_e164 = phone
        user.activation_used_at = now
        user.notify_whatsapp = True
        await db.commit()
        logger.info(f"[activation] user {user.user_id} activated")
        await send_whatsapp(to=phone, content_sid=cuenta_activada_sid(), content_variables={"1": user.display_name.split()[0]})
        return

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

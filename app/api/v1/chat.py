"""AI chat assistant endpoint."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_tools import execute_tool, tool_definitions_for
from app.core.config import settings
from app.core.motivos_catalogo import DESAMBIGUACION, MOTIVOS
from app.core.security import current_user
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

_motivos_text = "\n".join(f"- {m['codigo']}: {m['descripcion']}" for m in MOTIVOS)

SYSTEM_PROMPT = f"""Eres el asistente IA de Torre de Control, la plataforma de control operativo de ultima milla de Falabella, desarrollada por ValueData SpA.

Tu rol:
- Ayudar a operadores y administradores a entender el estado operativo
- Responder preguntas sobre conductores, vehiculos, empresas, documentos
- Clasificar motivos de no-entrega cuando se te pida
- Dar recomendaciones operativas basadas en datos

Reglas:
- Responde SIEMPRE en espanol chileno
- Se conciso y directo
- Usa los tools disponibles para consultar datos reales antes de responder
- LIMITA tool calls a maximo 3 por turno. Despues de 3 tool calls SINTETIZA la respuesta con lo que tengas, aunque te falten datos. Nunca repitas el mismo tool con los mismos parametros.
- Si no tienes suficiente informacion, pregunta o reporta lo que tengas
- Para clasificacion de motivos, analiza el comentario vs el motivo reportado y sugiere correccion si no coinciden
- Cuando un conductor o usuario pregunta sobre un folio cliente, un destinatario especifico, o una proxima entrega, llama SIEMPRE el tool `obtener_info_cliente_por_folio`. Si el cliente tiene `es_vip=true` o `notas_operativas` no vacias, mencionalo PROMINENTEMENTE en tu respuesta (ej: "Cliente VIP: razon X. Nota operativa: Y"). Esto es critico para que el conductor sepa como manejar la entrega.

CATALOGO OFICIAL DE MOTIVOS DE NO-ENTREGA (14 motivos):
{_motivos_text}

{DESAMBIGUACION}
"""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    reply: str
    tool_calls_made: list[str] = []


@router.post("", operation_id="chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    if not settings.azure_openai_endpoint or not settings.azure_openai_api_key.get_secret_value():
        return ChatResponse(reply="Azure OpenAI no esta configurado. Configura AZURE_OPENAI_ENDPOINT y AZURE_OPENAI_API_KEY.")

    from openai import AsyncAzureOpenAI

    client = AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),
        api_version=settings.azure_openai_api_version,
    )

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in body.messages:
        messages.append({"role": m.role, "content": m.content})

    tool_calls_made: list[str] = []
    tools = tool_definitions_for(user)

    for iteration in range(10):
        # After 3 tool calls, force the model to synthesize (no more tools).
        force_synthesize = iteration >= 3
        response = await client.chat.completions.create(
            model=settings.azure_openai_chat_deployment,
            messages=messages,
            tools=tools,
            tool_choice="none" if force_synthesize else "auto",
        )

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            messages.append(choice.message.model_dump())
            for tc in choice.message.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)
                logger.info(f"[chat] tool call: {fn_name}({fn_args})")
                tool_calls_made.append(fn_name)

                result = await execute_tool(db, fn_name, fn_args, actor=user)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            continue

        reply = choice.message.content or "No tengo respuesta para eso."
        return ChatResponse(reply=reply, tool_calls_made=tool_calls_made)

    return ChatResponse(reply="Se agotaron los intentos de procesamiento.", tool_calls_made=tool_calls_made)

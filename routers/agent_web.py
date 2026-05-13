"""Endpoint web del agente conversacional.

Reusa el dispatcher de WhatsApp (`twilio_inbound._dispatch`) para que el chat
de la app web hable con el MISMO FSM/comandos que el WA. Cada usuario tiene
una sesión por separado, persistida en `fpoc_whatsapp_sessions` con
phone_e164 = "web:<user_id>" para no chocar con números reales.

Endpoints:
  POST /api/agent/web/message   { message }  → { reply, kind }
  POST /api/agent/web/reset                  → { ok }
  GET  /api/agent/web/state                  → estado actual del FSM
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user


router = APIRouter(prefix="/api/agent", tags=["agent-web"])


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    reply: str
    kind: str = "agent"  # 'agent' | 'command' | 'empty'


def _web_phone(user: CurrentUser) -> str:
    """Phone sintético per-user para que el FSM tenga sesión propia."""
    return f"web:{user.user_id}"


def _identity_from_user(user: CurrentUser) -> dict:
    return {
        "user_id": user.user_id,
        "driver_id": user.driver_id,
        "empresa_id": user.empresa_id,
        "display_name": user.display_name,
        "role": user.role,
        "channel": "web",
    }


@router.post("/web/message", response_model=MessageResponse)
def web_message(
    req: MessageRequest,
    user: CurrentUser = Depends(current_user),
) -> MessageResponse:
    msg = req.message.strip()
    if not msg:
        return MessageResponse(reply="", kind="empty")

    # Import lazy para evitar ciclo en el arranque
    from routers.twilio_inbound import _dispatch

    phone = _web_phone(user)
    identity = _identity_from_user(user)
    try:
        reply = _dispatch(msg, identity, phone, profile_name=user.display_name)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[agent-web] dispatch falló: {e}")
        raise HTTPException(500, f"Error del agente: {e}")

    if reply is None or reply == "":
        return MessageResponse(
            reply="No pude entender el mensaje. Probá 'help' para ver comandos.",
            kind="empty",
        )
    return MessageResponse(reply=reply, kind="agent")


class ResetResponse(BaseModel):
    ok: bool


@router.post("/web/reset", response_model=ResetResponse)
def web_reset(user: CurrentUser = Depends(current_user)) -> ResetResponse:
    phone = _web_phone(user)
    try:
        from sims.whatsapp_agent import Session as _WaSession
        _WaSession.delete(phone)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[agent-web] reset falló: {e}")
        return ResetResponse(ok=False)
    return ResetResponse(ok=True)


class StateResponse(BaseModel):
    phone: str
    state: str
    role: Optional[str] = None
    identified_id: Optional[str] = None
    context: dict = {}


@router.get("/web/state", response_model=StateResponse)
def web_state(user: CurrentUser = Depends(current_user)) -> StateResponse:
    phone = _web_phone(user)
    try:
        from sims.whatsapp_agent import Session as _WaSession
        s = _WaSession.load(phone)
        return StateResponse(
            phone=s.phone,
            state=s.state,
            role=s.role,
            identified_id=s.identified_id,
            context=s.context or {},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[agent-web] state load falló: {e}")
        return StateResponse(phone=phone, state="idle")

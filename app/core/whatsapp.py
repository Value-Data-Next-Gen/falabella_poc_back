"""WhatsApp messaging via Twilio Content API."""
from __future__ import annotations

import asyncio
import json

from loguru import logger

from app.core.config import settings


def _send_blocking(
    to: str,
    content_sid: str | None,
    content_variables: dict | None,
    body: str | None,
) -> bool:
    """Blocking Twilio HTTP call — run via a thread, never on the event loop."""
    try:
        from twilio.rest import Client
        client = Client(
            settings.twilio_api_key_sid,
            settings.twilio_api_key_secret.get_secret_value(),
            settings.twilio_account_sid,
        )
        kwargs: dict = {
            "from_": settings.twilio_whatsapp_from,
            "to": f"whatsapp:{to}" if not to.startswith("whatsapp:") else to,
        }
        if content_sid:
            kwargs["content_sid"] = content_sid
            if content_variables:
                kwargs["content_variables"] = json.dumps(content_variables)
        elif body:
            kwargs["body"] = body
        msg = client.messages.create(**kwargs)
        logger.info(f"[whatsapp] sent {msg.sid} to {to}")
        return True
    except Exception as e:
        logger.error(f"[whatsapp] failed to send to {to}: {e}")
        return False


async def send_whatsapp(
    to: str,
    content_sid: str | None = None,
    content_variables: dict | None = None,
    body: str | None = None,
) -> bool:
    """Send a WhatsApp message. The Twilio SDK is synchronous, so the network
    call is offloaded to a worker thread — calling it directly on the single
    uvicorn event loop would block ALL requests + schedulers for the round-trip.
    """
    if settings.notifications_dry_run:
        logger.info(f"[whatsapp][DRY_RUN] to={to} content_sid={content_sid} body={body}")
        return True
    if not settings.twilio_account_sid or not settings.twilio_api_key_sid:
        logger.warning("[whatsapp] Twilio not configured, skipping")
        return False
    return await asyncio.to_thread(_send_blocking, to, content_sid, content_variables, body)

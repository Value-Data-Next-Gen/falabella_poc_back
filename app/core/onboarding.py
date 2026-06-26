"""WhatsApp onboarding helpers.

`send_invitation` pushes the Meta-approved INVITACION template to an invitee's
phone so the *system* drives onboarding (the supervisor clicks "invitar" and the
person just replies to activate — see the reply-to-activate flow in
`app.api.v1.twilio_webhook`). Best-effort: a Twilio hiccup never fails the
caller's request.
"""
from __future__ import annotations

from loguru import logger

from app.core.twilio_templates import invitacion_sid
from app.core.whatsapp import send_whatsapp


async def send_invitation(to: str | None, nombre: str | None) -> bool:
    """Send the INVITACION template to `to`. Returns True on a successful send.

    No-ops (returns False) when there's no phone. Swallows send errors so an
    invite failure doesn't 500 the create/regenerate endpoint that triggered it.
    """
    if not to:
        return False
    first = (nombre or "").split()[0] if (nombre or "").strip() else "👋"
    try:
        return await send_whatsapp(
            to=to, content_sid=invitacion_sid(), content_variables={"1": first}
        )
    except Exception as e:
        # Best-effort: an invite failure must never 500 the caller's request.
        logger.warning(f"[invite] send failed to {to}: {e}")
        return False

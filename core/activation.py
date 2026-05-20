"""Activation tokens para evitar el bloqueo de WhatsApp con users nuevos (error 63112).

Problema: WhatsApp Business API no abre la ventana 24h cuando el inbound del
usuario es respuesta a un template (Meta interpreta "ok" como reply al template
y no como mensaje user-initiated). El único path que abre la ventana es un
mensaje 100% user-initiated, sin template previo en la conversación reciente.

Solución: cuando el admin crea un user/driver/contacto generamos un token
corto y un link `https://wa.me/<sender>?text=ACTIVAR%20<TOKEN>`. El admin lo
comparte por fuera (email/Slack/etc.); el usuario hace click → se abre su
WhatsApp con el texto pre-rellenado → manda el mensaje. Como es un mensaje
unprompted, Meta abre la ventana y el bot puede responder freeform.

Alfabeto sin caracteres ambiguos (0/O, 1/I, etc.) para que sea legible en
papel/SMS si por algún motivo el link no llega bien.
"""
from __future__ import annotations

import os
import secrets


# 32 chars: A-Z (sin I, O) + 2-9 (sin 0, 1). 8 chars → 32**8 ≈ 1.1e12
# combinaciones, suficiente para que un brute-force vía WhatsApp sea inviable
# (Meta rate-limita mensajes inbound).
_ACTIVATION_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_activation_token(length: int = 8) -> str:
    """Token alfanumérico uppercase, fácil de tipear (sin 0/O ni I/1)."""
    return "".join(secrets.choice(_ACTIVATION_ALPHABET) for _ in range(length))


def build_activation_link(token: str | None) -> str:
    """Construye wa.me link con el sender configurado en TWILIO_WHATSAPP_FROM.

    Devuelve "" si no hay sender configurado o token vacío — el frontend
    interpreta string vacío como "infra de WhatsApp no configurada".
    """
    if not token:
        return ""
    sender = os.environ.get("TWILIO_WHATSAPP_FROM", "").replace("whatsapp:", "").lstrip("+")
    if not sender:
        return ""
    return f"https://wa.me/{sender}?text=ACTIVAR%20{token}"

"""Back-compat Twilio alias: /api/twilio/inbound (v1 / Twilio Console path) must
route to the v2 handler so inbound WhatsApp survives the cutover.

We assert routing/delegation via the signature gate (no real DB needed): with
validation on and no X-Twilio-Signature header, both the alias and the native
path reject identically. The /status alias returns 200 TwiML.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from app.api.v1 import twilio_webhook as tw
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


async def _dummy_db() -> AsyncIterator[None]:
    yield None


@pytest.fixture
def client(monkeypatch):
    # Deterministic: require signature validation.
    monkeypatch.setattr(tw.settings, "twilio_inbound_validate_signature", True)
    app.dependency_overrides[get_db] = _dummy_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_inbound_alias_exists_and_delegates(client):
    body = {"From": "whatsapp:+56900000000", "Body": "hola"}
    # No X-Twilio-Signature → handler rejects (proves the alias hit the handler).
    alias = client.post("/api/twilio/inbound", data=body)
    native = client.post("/api/v1/twilio/webhook", data=body)
    assert alias.status_code == 403, alias.text
    assert native.status_code == alias.status_code  # same handler, same behaviour


def test_status_callback_absorbed(client):
    r = client.post("/api/twilio/status", data={"MessageSid": "SM1", "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert "Response" in r.text  # empty TwiML

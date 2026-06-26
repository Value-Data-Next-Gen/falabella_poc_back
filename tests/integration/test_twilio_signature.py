"""Twilio inbound signature validation: a valid HMAC passes, a forged/missing
one is rejected (403). Regression guard for the no-op stub that accepted any
request with the header present."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.api.v1 import twilio_webhook as tw
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

_TOKEN = "test-auth-token-123"
_PUBLIC = "https://test.example"
_PATH = "/api/twilio/inbound"


@pytest_asyncio.fixture
async def sm() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def client(sm, monkeypatch):
    monkeypatch.setattr(tw.settings, "twilio_inbound_validate_signature", True)
    monkeypatch.setattr(tw.settings, "twilio_auth_token", SecretStr(_TOKEN))
    monkeypatch.setattr(tw.settings, "twilio_inbound_public_url", _PUBLIC)
    monkeypatch.setattr(tw.settings, "notifications_dry_run", True)

    async def _db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _valid_sig(params: dict) -> str:
    return RequestValidator(_TOKEN).compute_signature(_PUBLIC + _PATH, params)


def test_valid_signature_accepted(client):
    body = {"From": "whatsapp:+56900001111", "Body": "hola"}  # unknown number
    sig = _valid_sig(body)
    r = client.post(_PATH, data=body, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200, r.text  # passes validation → handler runs (unknown → TwiML)


def test_forged_signature_rejected(client):
    body = {"From": "whatsapp:+56900001111", "Body": "hola"}
    r = client.post(_PATH, data=body, headers={"X-Twilio-Signature": "deadbeef"})
    assert r.status_code == 403, r.text


def test_missing_signature_rejected(client):
    r = client.post(_PATH, data={"From": "whatsapp:+56900001111", "Body": "hola"})
    assert r.status_code == 403, r.text


def test_tampered_params_rejected(client):
    # Signature computed for one body, request sends a different one.
    sig = _valid_sig({"From": "whatsapp:+56900001111", "Body": "hola"})
    r = client.post(_PATH, data={"From": "whatsapp:+56900001111", "Body": "TAMPERED"},
                    headers={"X-Twilio-Signature": sig})
    assert r.status_code == 403, r.text

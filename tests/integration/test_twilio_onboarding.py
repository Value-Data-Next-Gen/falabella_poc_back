"""Hybrid WhatsApp onboarding: reply-to-activate (phone match), token
activation, opt-out, and the unknown-number guidance path."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import app.api.v1.twilio_webhook as wh
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def seeded() -> AsyncIterator[dict]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        # invited-but-not-activated driver (phone known, opted_in_at NULL)
        db.add(Driver(driver_id="DRV-1", empresa_id=1, nombre="Ana Pérez",
                      phone_e164="+56911111111", opted_in_at=None, activo=True))
        await db.commit()
    yield {"sm": sm}
    await engine.dispose()


@pytest.fixture
def client(seeded, monkeypatch):
    sm = seeded["sm"]
    sent: list[dict] = []

    async def _db():
        async with sm() as s:
            yield s

    async def _no_validate(_request):
        return None

    async def _fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(wh, "_validate_signature", _no_validate)
    monkeypatch.setattr(wh, "send_whatsapp", _fake_send)
    app.dependency_overrides[get_db] = _db
    c = TestClient(app)
    c._sent = sent  # type: ignore[attr-defined]
    c._sm = sm  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


async def _driver(sm, did="DRV-1") -> Driver:
    async with sm() as s:
        return (await s.execute(select(Driver).where(Driver.driver_id == did))).scalar_one()


@pytest.mark.asyncio
async def test_reply_activates_pending_invitee(client):
    r = client.post("/api/v1/twilio/webhook", data={"From": "whatsapp:+56911111111", "Body": "ok"})
    assert r.status_code == 200, r.text
    d = await _driver(client._sm)
    assert d.opted_in_at is not None              # activated by reply
    assert d.notify_whatsapp is True
    # welcome template sent
    assert any(s.get("content_sid") == wh.cuenta_activada_sid() for s in client._sent)


@pytest.mark.asyncio
async def test_stop_opts_out(client):
    # activate first, then opt out
    client.post("/api/v1/twilio/webhook", data={"From": "whatsapp:+56911111111", "Body": "ok"})
    r = client.post("/api/v1/twilio/webhook", data={"From": "whatsapp:+56911111111", "Body": "STOP"})
    assert r.status_code == 200
    d = await _driver(client._sm)
    assert d.opted_in_at is None and d.notify_whatsapp is False


@pytest.mark.asyncio
async def test_unknown_number_gets_guidance(client):
    r = client.post("/api/v1/twilio/webhook", data={"From": "whatsapp:+56999999999", "Body": "hola"})
    assert r.status_code == 200
    assert client._sent and "registrado" in client._sent[-1].get("body", "").lower()


@pytest.mark.asyncio
async def test_token_activation_still_works(client):
    # blank the phone so only the token can match, then activate via wa.me path
    async with client._sm() as s:
        d = (await s.execute(select(Driver).where(Driver.driver_id == "DRV-1"))).scalar_one()
        d.activation_token = "tok-123"
        d.phone_e164 = None
        await s.commit()
    r = client.post("/api/v1/twilio/webhook",
                    data={"From": "whatsapp:+56922222222", "Body": "ACTIVAR tok-123"})
    assert r.status_code == 200
    d = await _driver(client._sm)
    assert d.opted_in_at is not None and d.phone_e164 == "+56922222222"

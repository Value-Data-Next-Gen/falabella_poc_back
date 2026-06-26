"""POST /operacion/visitas/{id}/notify-driver — targeted late-delivery ping."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import app.api.v1.operacion as opmod
from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def seeded() -> AsyncIterator[tuple]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    now = datetime.now(UTC)
    async with sm() as s:
        s.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await s.flush()
        s.add(Driver(driver_id="D1", empresa_id=1, nombre="Ana", phone_e164="+56911111111", opted_in_at=now, activo=True))
        s.add(Driver(driver_id="D2", empresa_id=1, nombre="Sin WA", phone_e164=None, opted_in_at=None, activo=True))
        s.add(Vehicle(vehicle_id=1, empresa_id=1, nombre="Cam", plate="AA-11", activo=True))
        dia = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 18), estado="EN_CURSO")
        s.add(dia)
        await s.flush()
        s.add_all([
            Ruta(ruta_id=1, dia_id=dia.dia_id, driver_id="D1", vehicle_id=1, orden=1),
            Ruta(ruta_id=2, dia_id=dia.dia_id, driver_id="D2", vehicle_id=1, orden=2),
        ])
        await s.flush()
        s.add_all([
            Visita(visita_id=10, dia_id=dia.dia_id, empresa_id=1, ruta_id=1, orden=1, cliente_nombre="Cli", direccion="d", estado="pendiente", folio_cliente="F1"),
            Visita(visita_id=11, dia_id=dia.dia_id, empresa_id=1, ruta_id=2, orden=1, cliente_nombre="Cli2", direccion="d", estado="pendiente"),
            Visita(visita_id=12, dia_id=dia.dia_id, empresa_id=1, ruta_id=None, orden=2, cliente_nombre="Cli3", direccion="d", estado="pendiente"),
        ])
        await s.commit()
    yield sm
    await engine.dispose()


def _client(sm, monkeypatch):
    sent = []

    async def _db():
        async with sm() as s:
            yield s

    async def _admin() -> User:
        u = User(user_id=1, email="a@td.cl", password_hash="x", display_name="A", role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u

    async def _fake_send(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(opmod, "send_whatsapp", _fake_send)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _admin
    c = TestClient(app)
    c._sent = sent  # type: ignore[attr-defined]
    return c


def test_notify_driver_sends_targeted(seeded, monkeypatch):
    c = _client(seeded, monkeypatch)
    try:
        r = c.post("/api/v1/operacion/visitas/10/notify-driver", json={"motivo": "ATRASO"})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["sent"] is True and b["driver_id"] == "D1"
        cv = c._sent[0]["content_variables"]
        assert c._sent[0]["to"] == "+56911111111"
        assert cv["2"] == "ATRASO" and cv["4"] == "Ana" and cv["5"] == "Cli"
    finally:
        app.dependency_overrides.clear()


def test_notify_driver_without_whatsapp(seeded, monkeypatch):
    c = _client(seeded, monkeypatch)
    try:
        b = c.post("/api/v1/operacion/visitas/11/notify-driver", json={}).json()
        assert b["sent"] is False and "WhatsApp" in b["info"]
        assert c._sent == []
    finally:
        app.dependency_overrides.clear()


def test_notify_driver_no_route(seeded, monkeypatch):
    c = _client(seeded, monkeypatch)
    try:
        b = c.post("/api/v1/operacion/visitas/12/notify-driver", json={}).json()
        assert b["sent"] is False and "conductor" in b["info"].lower()
    finally:
        app.dependency_overrides.clear()

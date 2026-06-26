"""POST /api/v1/clientes/{id}/retener — 'No entregar' flag + driver WhatsApp alert."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import app.api.v1.clientes as clientes_mod
from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.cliente import Cliente
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
        s.add(Cliente(cliente_id=1, nombre="Cliente Sospechoso"))
        s.add(Driver(driver_id="D1", empresa_id=1, nombre="Ana", phone_e164="+56911111111",
                     opted_in_at=now, notify_whatsapp=True, activo=True))
        s.add(Vehicle(vehicle_id=1, empresa_id=1, nombre="Camión", plate="ABCD-12", activo=True))
        dia = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 17), estado="EN_CURSO")
        s.add(dia)
        await s.flush()
        ruta = Ruta(ruta_id=1, dia_id=dia.dia_id, driver_id="D1", vehicle_id=1, orden=1)
        s.add(ruta)
        await s.flush()
        s.add(Visita(dia_id=dia.dia_id, empresa_id=1, ruta_id=1, orden=1, cliente_id=1,
                     cliente_nombre="Cliente Sospechoso", direccion="d", estado="pendiente"))
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
def client(seeded, monkeypatch):
    sm = seeded
    sent: list[dict] = []

    async def _db():
        async with sm() as s:
            yield s

    async def _admin() -> User:
        u = User(user_id=1, email="a@td.cl", password_hash="x", display_name="A",
                 role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u

    async def _fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(clientes_mod, "send_whatsapp", _fake_send)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _admin
    c = TestClient(app)
    c._sent = sent  # type: ignore[attr-defined]
    c._sm = sm  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def test_retener_sets_flag_and_alerts_driver(client):
    r = client.post("/api/v1/clientes/1/retener",
                    json={"retener": True, "motivo": "Posible fraude/robo"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["retener"] is True
    assert b["visitas_afectadas"] == 1
    assert b["avisos_enviados"] == 1
    # the WhatsApp alert reused ALERTA_MOTIVO with NO ENTREGAR
    assert client._sent and client._sent[0]["content_variables"]["2"] == "NO ENTREGAR"
    assert client._sent[0]["content_variables"]["6"] == "Posible fraude/robo"
    assert client._sent[0]["to"] == "+56911111111"


def test_unretener_clears_and_no_alert(client):
    client.post("/api/v1/clientes/1/retener", json={"retener": True, "motivo": "x"})
    client._sent.clear()
    r = client.post("/api/v1/clientes/1/retener", json={"retener": False})
    assert r.status_code == 200
    assert r.json()["retener"] is False
    assert client._sent == []  # no alert when clearing


def test_retener_persists_for_bot_lookup(client):
    client.post("/api/v1/clientes/1/retener", json={"retener": True, "motivo": "robo"})
    g = client.get("/api/v1/clientes/1")
    assert g.json()["retener"] is True
    assert g.json()["retener_motivo"] == "robo"

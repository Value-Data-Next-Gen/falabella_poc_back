"""Centro de Control: counters, route health, exception queue + alert ack."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def seeded() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    now = datetime(2026, 6, 17, 15, 0, tzinfo=UTC)
    async with sm() as s:
        s.add(SimClock(id=1, sim_now=now, speed=1, running=True, last_tick_at=now))
        s.add(User(user_id=7, email="a@td.cl", password_hash="x", display_name="Operador A",
                   role="falabella_admin", activo=True))
        s.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await s.flush()
        s.add(Cliente(cliente_id=1, nombre="Bloqueado", retener=True, retener_motivo="robo"))
        s.add(Driver(driver_id="D1", empresa_id=1, nombre="Ana", activo=True))
        dia = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 17), estado="EN_CURSO")
        s.add(dia)
        await s.flush()
        r = Ruta(ruta_id=1, dia_id=dia.dia_id, driver_id="D1", orden=1)
        s.add(r)
        await s.flush()
        # 1 entregado, 1 overdue pendiente, 1 VIP pendiente, 1 blocked pendiente
        s.add_all([
            Visita(dia_id=dia.dia_id, empresa_id=1, ruta_id=1, orden=1, cliente_nombre="a", direccion="d", estado="entregado"),
            Visita(dia_id=dia.dia_id, empresa_id=1, ruta_id=1, orden=2, cliente_nombre="b", direccion="d", estado="pendiente", eta_estimada=now - timedelta(minutes=30)),
            Visita(dia_id=dia.dia_id, empresa_id=1, ruta_id=1, orden=3, cliente_nombre="c", direccion="d", estado="pendiente", es_vip=1),
            Visita(dia_id=dia.dia_id, empresa_id=1, ruta_id=1, orden=4, cliente_id=1, cliente_nombre="Bloqueado", direccion="d", estado="pendiente"),
        ])
        s.add(Alert(tipo="eta_breach", severity="critica", empresa_id=1, dia_id=dia.dia_id,
                    descripcion="atraso grave", estado="abierta", created_at=now - timedelta(minutes=10)))
        await s.commit()
    yield sm
    await engine.dispose()


def _client(sm, role="falabella_admin", empresa_ids=None):
    async def _db():
        async with sm() as s:
            yield s

    async def _user() -> User:
        u = User(user_id=7, email="a@td.cl", password_hash="x", display_name="Operador A", role=role, activo=True)
        u._empresa_ids = empresa_ids or []  # type: ignore[attr-defined]
        return u

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    return TestClient(app)


def test_command_center_counters_and_exceptions(seeded):
    c = _client(seeded)
    try:
        b = c.get("/api/v1/operacion/command-center").json()
        co = b["counters"]
        assert co["rutas_activas"] == 1
        assert co["visitas_pendientes"] == 3
        assert co["atrasadas"] == 1
        assert co["vip_pendientes"] == 1
        assert co["bloqueados"] == 1
        assert co["alertas_abiertas"] == 1
        assert b["routes"][0]["atrasadas"] == 1
        assert b["exceptions"][0]["severity"] == "critica"
        assert b["exceptions"][0]["edad_min"] == 10
    finally:
        app.dependency_overrides.clear()


def test_ack_and_release_alert(seeded):
    c = _client(seeded)
    try:
        aid = c.get("/api/v1/operacion/command-center").json()["exceptions"][0]["alert_id"]
        r = c.post(f"/api/v1/alerts/{aid}/ack")
        assert r.status_code == 200 and r.json()["owner_user_id"] == 7
        # now command-center shows the owner
        ex = c.get("/api/v1/operacion/command-center").json()["exceptions"][0]
        assert ex["owner_nombre"] == "Operador A"
        rel = c.post(f"/api/v1/alerts/{aid}/release")
        assert rel.json()["owner_user_id"] is None
    finally:
        app.dependency_overrides.clear()


def test_manager_scoped_empty(seeded):
    c = _client(seeded, role="transport_manager", empresa_ids=[999])
    try:
        b = c.get("/api/v1/operacion/command-center").json()
        assert b["counters"]["rutas_activas"] == 0
        assert b["exceptions"] == []
    finally:
        app.dependency_overrides.clear()

"""CR-021 — tenant isolation tests for /api/v1/operacion/*.

Reproduces the audit's findings (cross-tenant POSTs were accepted) and asserts
they now return 403. The DB is an in-memory SQLite spun up per test session.

What we verify (transport_manager scoped to empresa 1):
  * GET /dias lists only dias from empresa 1 (not empresa 2).
  * GET /dias/{day2_id} → 403 (cross-empresa).
  * GET /dias/{day1_id} → 200.
  * POST /dias/{day2_id}/visitas → 403.
  * POST /dias/{day2_id}/rutas → 403.
  * POST /dias/{day2_id}/transition → 403.
  * POST /dias/{day1_id}/transition → 200 (own empresa).
  * DELETE /dias/{day1_id} in BORRADOR → 204.
  * DELETE /dias/{day2_id} → 403.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Set DB_TEST_URL before any app.* import so the engine picks SQLite.
os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
from app.core.security import current_user

# Importing the package side-effect registers every model on Base.metadata.
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.models.vehicle import Vehicle
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Build a fresh in-memory SQLite, create all tables from metadata.

    StaticPool keeps the same in-memory DB across connections in the test.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sessionmaker
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def seeded(_engine: async_sessionmaker[AsyncSession]) -> dict:
    """Seed 2 empresas, 2 drivers (one per empresa), 2 dias (BORRADOR),
    and 2 users (admin + transport_manager scoped to empresa 1).
    """
    async with _engine() as db:
        e1 = Empresa(empresa_id=1, nombre="Empresa Uno", activo=True)
        e2 = Empresa(empresa_id=2, nombre="Empresa Dos", activo=True)
        db.add_all([e1, e2])
        await db.flush()

        v1 = Vehicle(vehicle_id=1, empresa_id=1, nombre="V1", plate="ABC11", activo=True)
        v2 = Vehicle(vehicle_id=2, empresa_id=2, nombre="V2", plate="ABC22", activo=True)
        db.add_all([v1, v2])
        await db.flush()

        d1 = Driver(driver_id="DRV-01001", empresa_id=1, nombre="Driver Uno", activo=True)
        d2 = Driver(driver_id="DRV-02001", empresa_id=2, nombre="Driver Dos", activo=True)
        db.add_all([d1, d2])
        await db.flush()

        day1 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="BORRADOR")
        day2 = DiaOperativo(empresa_id=2, fecha=date(2026, 6, 1), estado="BORRADOR")
        db.add_all([day1, day2])
        await db.flush()

        admin = User(
            user_id=10,
            email="admin@td.cl",
            password_hash="x",
            display_name="Admin",
            role="falabella_admin",
            activo=True,
        )
        mgr = User(
            user_id=20,
            email="mgr1@td.cl",
            password_hash="x",
            display_name="Mgr E1",
            role="transport_manager",
            activo=True,
        )
        db.add_all([admin, mgr])
        await db.flush()

        # mgr can see empresa 1 only.
        db.add(UserEmpresa(user_id=20, empresa_id=1))
        await db.commit()

        return {
            "engine_sm": _engine,
            "day1_id": day1.dia_id,
            "day2_id": day2.dia_id,
            "admin_id": 10,
            "mgr_id": 20,
            "driver1_id": "DRV-01001",
            "driver2_id": "DRV-02001",
        }


def _override_user(role: str, user_id: int, empresa_ids: list[int]):
    """Returns a dependency override for `current_user` returning a stub User."""

    async def _stub() -> User:
        u = User(
            user_id=user_id,
            email=f"{role}@td.cl",
            password_hash="x",
            display_name=role,
            role=role,
            activo=True,
        )
        u._empresa_ids = empresa_ids  # type: ignore[attr-defined]
        return u

    return _stub


@pytest.fixture
def client_factory(seeded: dict):
    """Build a TestClient with `get_db` + `current_user` overridden per call."""
    sessionmaker = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    def make(role: str, user_id: int, empresa_ids: list[int]) -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _override_user(role, user_id, empresa_ids)
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

def test_list_dias_filters_by_empresa_for_transport_manager(client_factory, seeded):
    """transport_manager only sees dias of empresa 1, not empresa 2."""
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.get("/api/v1/operacion/dias")
    assert r.status_code == 200
    ids = {row["dia_id"] for row in r.json()}
    assert seeded["day1_id"] in ids
    assert seeded["day2_id"] not in ids


def test_get_dia_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.get(f"/api/v1/operacion/dias/{seeded['day2_id']}")
    assert r.status_code == 403, r.text


def test_get_dia_own_tenant_200(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.get(f"/api/v1/operacion/dias/{seeded['day1_id']}")
    assert r.status_code == 200, r.text


def test_create_visita_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    body = {
        "cliente_nombre": "Pwn",
        "direccion": "Falsa 123",
        "n_bultos": 1,
        "es_vip": False,
    }
    r = c.post(f"/api/v1/operacion/dias/{seeded['day2_id']}/visitas", json=body)
    assert r.status_code == 403, r.text


def test_create_ruta_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['day2_id']}/rutas",
        json={"driver_id": seeded["driver2_id"]},
    )
    assert r.status_code == 403, r.text


def test_create_ruta_with_other_empresas_driver_400(client_factory, seeded):
    """Even within own dia, assigning a driver of another empresa → 400."""
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['day1_id']}/rutas",
        json={"driver_id": seeded["driver2_id"]},
    )
    assert r.status_code == 400, r.text


def test_transition_dia_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['day2_id']}/transition?nuevo_estado=VALIDADO"
    )
    assert r.status_code == 403, r.text


def test_transition_dia_own_tenant_200(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['day1_id']}/transition?nuevo_estado=VALIDADO"
    )
    assert r.status_code == 200, r.text


def test_delete_dia_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.delete(f"/api/v1/operacion/dias/{seeded['day2_id']}")
    assert r.status_code == 403, r.text


def test_delete_dia_own_tenant_borrador_204(client_factory, seeded):
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    r = c.delete(f"/api/v1/operacion/dias/{seeded['day1_id']}")
    assert r.status_code == 204, r.text


def test_delete_dia_validado_409(client_factory, seeded):
    """Once a dia has moved past BORRADOR, DELETE returns 409."""
    c = client_factory("transport_manager", seeded["mgr_id"], [1])
    # Move to VALIDADO first.
    t = c.post(
        f"/api/v1/operacion/dias/{seeded['day1_id']}/transition?nuevo_estado=VALIDADO"
    )
    assert t.status_code == 200, t.text
    r = c.delete(f"/api/v1/operacion/dias/{seeded['day1_id']}")
    assert r.status_code == 409, r.text


def test_delete_dia_ops_role_forbidden(client_factory, seeded):
    """falabella_ops can read everything but cannot delete dias (admin only)."""
    c = client_factory("falabella_ops", 30, [])
    r = c.delete(f"/api/v1/operacion/dias/{seeded['day1_id']}")
    assert r.status_code == 403, r.text


def test_admin_sees_all_dias(client_factory, seeded):
    c = client_factory("falabella_admin", seeded["admin_id"], [])
    r = c.get("/api/v1/operacion/dias")
    assert r.status_code == 200
    ids = {row["dia_id"] for row in r.json()}
    assert seeded["day1_id"] in ids
    assert seeded["day2_id"] in ids

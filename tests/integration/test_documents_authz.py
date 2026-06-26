"""Tenant authorization + input validation for /api/v1/documents/*.

The document endpoints were previously authenticated but NOT authorized: any
logged-in user could read/download/delete another empresa's driver/vehicle PII
by guessing entity_type/entity_id. These tests assert the fix:

  * cross-empresa access → 403 (conductor / vehiculo / empresa)
  * own-empresa access → 200
  * unknown entity → 404
  * malformed entity_id (path-traversal shaped) → 400
  * falabella_admin sees everything

In-memory SQLite spun up per test, mirroring test_operacion_scope.py.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
from app.core.security import current_user
from app.db import models  # noqa: F401  (registers models on Base.metadata)
from app.db.base import Base
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture(scope="function")
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
    async with _engine() as db:
        db.add_all([
            Empresa(empresa_id=1, nombre="Empresa Uno", activo=True),
            Empresa(empresa_id=2, nombre="Empresa Dos", activo=True),
        ])
        await db.flush()
        db.add_all([
            Vehicle(vehicle_id=1, empresa_id=1, nombre="V1", plate="ABC11", activo=True),
            Vehicle(vehicle_id=2, empresa_id=2, nombre="V2", plate="ABC22", activo=True),
        ])
        db.add_all([
            Driver(driver_id="DRV-01001", empresa_id=1, nombre="Driver Uno", activo=True),
            Driver(driver_id="DRV-02001", empresa_id=2, nombre="Driver Dos", activo=True),
        ])
        await db.commit()
    return {"engine_sm": _engine}


def _override_user(role: str, user_id: int, empresa_ids: list[int]):
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


# ── transport_manager scoped to empresa 1 ──

def test_list_docs_cross_tenant_conductor_403(client_factory):
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/documents/conductor/DRV-02001")  # empresa 2's driver
    assert r.status_code == 403, r.text


def test_list_docs_own_tenant_conductor_200(client_factory):
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/documents/conductor/DRV-01001")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_docs_cross_tenant_vehiculo_403(client_factory):
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/documents/vehiculo/2")  # empresa 2's vehicle
    assert r.status_code == 403, r.text


def test_list_docs_cross_tenant_empresa_403(client_factory):
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/documents/empresa/2")
    assert r.status_code == 403, r.text


def test_list_docs_own_empresa_200(client_factory):
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/documents/empresa/1")
    assert r.status_code == 200, r.text


def test_unknown_entity_404(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/documents/conductor/DRV-99999")
    assert r.status_code == 404, r.text


def test_malformed_entity_id_400(client_factory):
    """A traversal-shaped / invalid entity_id is rejected before any lookup."""
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/documents/conductor/bad.id")  # '.' fails ^[A-Za-z0-9_-]+$
    assert r.status_code == 400, r.text


def test_admin_sees_any_tenant(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/documents/conductor/DRV-02001")
    assert r.status_code == 200, r.text

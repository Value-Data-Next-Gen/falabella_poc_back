"""GET /api/v1/onboarding — scoped activation overview."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
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
    now = datetime.now(UTC)
    async with sm() as s:
        s.add_all([Empresa(empresa_id=1, nombre="E1", activo=True),
                   Empresa(empresa_id=2, nombre="E2", activo=True)])
        await s.flush()
        # empresa 1: 1 activated + 1 pending driver; empresa 2: 1 pending driver
        s.add_all([
            Driver(driver_id="D1A", empresa_id=1, nombre="a", activation_token="t1", opted_in_at=now, activo=True),
            Driver(driver_id="D1B", empresa_id=1, nombre="b", activation_token="t2", opted_in_at=None, activo=True),
            Driver(driver_id="D2A", empresa_id=2, nombre="c", activation_token="t3", opted_in_at=None, activo=True),
            EmpresaContacto(contact_id=1, empresa_id=1, nombre="jefe1", rol="jefe", activation_token="t4", opted_in_at=None, activo=True),
        ])
        await s.commit()
    yield sm
    await engine.dispose()


def _client(sm, role, empresa_ids):
    async def _db():
        async with sm() as s:
            yield s

    async def _user() -> User:
        u = User(user_id=1, email="x@td.cl", password_hash="x", display_name="X", role=role, activo=True)
        u._empresa_ids = empresa_ids  # type: ignore[attr-defined]
        return u

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    return TestClient(app)


def test_admin_sees_all_with_counts(seeded):
    c = _client(seeded, "falabella_admin", [])
    try:
        r = c.get("/api/v1/onboarding")
        assert r.status_code == 200, r.text
        b = r.json()
        # 3 drivers + 1 contacto = 4; 1 activated (D1A)
        assert b["total"] == 4
        assert b["activados"] == 1
        assert b["pendientes"] == 3
    finally:
        app.dependency_overrides.clear()


def test_manager_scoped_to_empresa(seeded):
    c = _client(seeded, "transport_manager", [1])
    try:
        b = c.get("/api/v1/onboarding").json()
        # empresa 1 only: D1A + D1B + jefe1 = 3
        assert b["total"] == 3
        assert {i["empresa_id"] for i in b["items"]} == {1}
    finally:
        app.dependency_overrides.clear()


def test_solo_pendientes_filters(seeded):
    c = _client(seeded, "falabella_admin", [])
    try:
        b = c.get("/api/v1/onboarding", params={"solo_pendientes": "true"}).json()
        assert b["total"] == 4  # totals always reflect everyone
        assert all(not i["activado"] for i in b["items"])
        assert len(b["items"]) == 3
    finally:
        app.dependency_overrides.clear()

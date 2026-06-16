"""CR-022 Part A — integration tests for /api/v1/alerts/* endpoints.

Verifies:
  * POST /manual + GET /{id} + PATCH /{id} → 201 / 200 / 200.
  * Cross-tenant POST /manual → 403.
  * Cross-tenant GET /{id} → 403.
  * Cross-tenant PATCH /{id} → 403.
  * list endpoint scoped to empresa for transport_manager.
  * PATCH on already-resolved alert → 409.
  * Dispatch endpoint (admin only) → 403 for transport_manager.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(_engine: async_sessionmaker[AsyncSession]) -> dict:
    async with _engine() as db:
        db.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        await db.flush()
        day1 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        day2 = DiaOperativo(empresa_id=2, fecha=date(2026, 6, 1), estado="EN_CURSO")
        db.add_all([day1, day2])
        await db.flush()

        # pre-existing alerts: one in each empresa for list-scope testing.
        a1 = Alert(
            tipo="eta_breach", severity="alta", empresa_id=1, dia_id=day1.dia_id,
            descripcion="visita atrasada e1", estado="abierta",
        )
        a2 = Alert(
            tipo="eta_breach", severity="alta", empresa_id=2, dia_id=day2.dia_id,
            descripcion="visita atrasada e2", estado="abierta",
        )
        db.add_all([a1, a2])

        # Users.
        db.add_all([
            User(user_id=10, email="adm@td.cl", password_hash="x",
                 display_name="Adm", role="falabella_admin", activo=True),
            User(user_id=20, email="mgr@td.cl", password_hash="x",
                 display_name="Mgr", role="transport_manager", activo=True),
        ])
        await db.flush()
        db.add(UserEmpresa(user_id=20, empresa_id=1))
        await db.commit()

        return {
            "engine_sm": _engine,
            "alert1_id": a1.alert_id,
            "alert2_id": a2.alert_id,
            "day1_id": day1.dia_id,
            "day2_id": day2.dia_id,
        }


def _user_override(role: str, user_id: int, empresa_ids: list[int]):
    async def _stub() -> User:
        u = User(
            user_id=user_id, email=f"{role}@td.cl", password_hash="x",
            display_name=role, role=role, activo=True,
        )
        u._empresa_ids = empresa_ids  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(seeded: dict):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    def make(role: str, user_id: int, empresa_ids: list[int]) -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _user_override(role, user_id, empresa_ids)
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

def test_list_alerts_scoped_to_empresa_for_transport_manager(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/alerts")
    assert r.status_code == 200, r.text
    ids = {row["alert_id"] for row in r.json()}
    assert seeded["alert1_id"] in ids
    assert seeded["alert2_id"] not in ids


def test_admin_lists_all_alerts(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/alerts")
    assert r.status_code == 200, r.text
    ids = {row["alert_id"] for row in r.json()}
    assert seeded["alert1_id"] in ids
    assert seeded["alert2_id"] in ids


def test_get_alert_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r = c.get(f"/api/v1/alerts/{seeded['alert2_id']}")
    assert r.status_code == 403, r.text


def test_get_alert_own_tenant_200(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r = c.get(f"/api/v1/alerts/{seeded['alert1_id']}")
    assert r.status_code == 200, r.text


def test_create_manual_alert_own_tenant_201(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    body = {
        "empresa_id": 1, "severity": "media",
        "dia_id": seeded["day1_id"],
        "descripcion": "alerta manual de prueba",
    }
    r = c.post("/api/v1/alerts/manual", json=body)
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["tipo"] == "manual"
    assert payload["severity"] == "media"
    assert payload["estado"] == "abierta"


def test_create_manual_alert_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    body = {
        "empresa_id": 2,  # not in scope
        "severity": "media", "descripcion": "exploit",
    }
    r = c.post("/api/v1/alerts/manual", json=body)
    assert r.status_code == 403, r.text


def test_patch_alert_resuelta_own_tenant_200(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r = c.patch(
        f"/api/v1/alerts/{seeded['alert1_id']}",
        json={"estado": "resuelta"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["estado"] == "resuelta"
    assert body["resolved_by_user_id"] == 20
    assert body["resolved_at"] is not None


def test_patch_alert_already_resolved_409(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r1 = c.patch(
        f"/api/v1/alerts/{seeded['alert1_id']}",
        json={"estado": "resuelta"},
    )
    assert r1.status_code == 200
    r2 = c.patch(
        f"/api/v1/alerts/{seeded['alert1_id']}",
        json={"estado": "descartada"},
    )
    assert r2.status_code == 409, r2.text


def test_patch_alert_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r = c.patch(
        f"/api/v1/alerts/{seeded['alert2_id']}",
        json={"estado": "resuelta"},
    )
    assert r.status_code == 403, r.text


def test_dispatch_endpoint_admin_only(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])
    r = c.post(f"/api/v1/alerts/{seeded['alert1_id']}/dispatch")
    assert r.status_code == 403, r.text


def test_dispatch_endpoint_admin_200(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    r = c.post(f"/api/v1/alerts/{seeded['alert1_id']}/dispatch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["alert_id"] == seeded["alert1_id"]
    assert body["dry_run"] is True  # conftest sets NOTIFICATIONS_DRY_RUN=true

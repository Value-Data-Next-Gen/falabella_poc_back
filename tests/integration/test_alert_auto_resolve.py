"""CR-025 — Auto-resolve alerts when a dia transitions to CERRADO + filter
of closed-dia alerts on GET /alerts.

Reproduces the zombie-alerts bug observed in production (29 alerts hanging
'abierta'/'notificada' on a CERRADO dia) and verifies the fix.

What we verify:
  1. Transition to CERRADO marks all 'abierta'/'notificada' alerts on the dia
     as 'resuelta' with the auto-resolve suffix on `descripcion`.
  2. Transition to any non-CERRADO state does NOT touch alerts.
  3. GET /alerts (default) hides alerts whose dia is CERRADO.
  4. GET /alerts?incluir_cerradas=true returns them anyway.
  5. Manual alerts with dia_id=NULL are visible regardless of the flag.
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
    """Seed:
      * Empresa 1.
      * 2 dias EN_CURSO (day_en_curso, day_other_state) and 1 dia CERRADO
        (day_cerrado).
      * On day_en_curso: one 'abierta' + one 'notificada' alert.
      * On day_other_state: one 'abierta' alert (for the no-side-effects test).
      * On day_cerrado: one 'resuelta' alert (already closed previously) so we
        can verify the GET filter without depending on the cron.
      * One manual alert with dia_id=NULL (always visible).
      * Admin user.
    """
    async with _engine() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()

        day_en_curso = DiaOperativo(
            empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO"
        )
        day_other = DiaOperativo(
            empresa_id=1, fecha=date(2026, 6, 2), estado="VALIDADO"
        )
        day_cerrado = DiaOperativo(
            empresa_id=1, fecha=date(2026, 6, 3), estado="CERRADO"
        )
        db.add_all([day_en_curso, day_other, day_cerrado])
        await db.flush()

        a_open = Alert(
            tipo="eta_breach", severity="alta", empresa_id=1,
            dia_id=day_en_curso.dia_id,
            descripcion="visita atrasada A", estado="abierta",
        )
        a_notified = Alert(
            tipo="eta_preview", severity="media", empresa_id=1,
            dia_id=day_en_curso.dia_id,
            descripcion="pre-aviso B", estado="notificada",
        )
        a_other = Alert(
            tipo="eta_breach", severity="alta", empresa_id=1,
            dia_id=day_other.dia_id,
            descripcion="visita atrasada C", estado="abierta",
        )
        a_on_cerrado = Alert(
            tipo="eta_breach", severity="alta", empresa_id=1,
            dia_id=day_cerrado.dia_id,
            descripcion="ya cerrada D", estado="resuelta",
        )
        a_manual_no_dia = Alert(
            tipo="manual", severity="media", empresa_id=1, dia_id=None,
            descripcion="manual sin dia", estado="abierta",
        )
        db.add_all([a_open, a_notified, a_other, a_on_cerrado, a_manual_no_dia])

        db.add(
            User(
                user_id=10, email="adm@td.cl", password_hash="x",
                display_name="Adm", role="falabella_admin", activo=True,
            )
        )
        await db.flush()
        db.add(UserEmpresa(user_id=10, empresa_id=1))
        await db.commit()

        return {
            "engine_sm": _engine,
            "day_en_curso_id": day_en_curso.dia_id,
            "day_other_id": day_other.dia_id,
            "day_cerrado_id": day_cerrado.dia_id,
            "alert_open_id": a_open.alert_id,
            "alert_notified_id": a_notified.alert_id,
            "alert_other_id": a_other.alert_id,
            "alert_on_cerrado_id": a_on_cerrado.alert_id,
            "alert_manual_no_dia_id": a_manual_no_dia.alert_id,
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
# 1. transition CERRADO auto-resolves alerts
# ----------------------------------------------------------------------------

def test_transition_to_cerrado_auto_resolves_pending_alerts(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['day_en_curso_id']}"
        f"/transition?nuevo_estado=CERRADO"
    )
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "CERRADO"

    # Verify both alerts are now 'resuelta' with the auto suffix.
    for alert_id in (seeded["alert_open_id"], seeded["alert_notified_id"]):
        a = c.get(f"/api/v1/alerts/{alert_id}?incluir_cerradas=true")
        assert a.status_code == 200, a.text
        body = a.json()
        assert body["estado"] == "resuelta", body
        assert body["resolved_at"] is not None
        assert body["resolved_by_user_id"] == 10
        assert "[auto-resuelta: dia cerrado]" in body["descripcion"], body


# ----------------------------------------------------------------------------
# 2. transition to non-CERRADO does NOT touch alerts
# ----------------------------------------------------------------------------

def test_transition_to_non_cerrado_does_not_touch_alerts(client_factory, seeded):
    """Moving day_other from VALIDADO to EN_CURSO must not touch its alert."""
    c = client_factory("falabella_admin", 10, [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['day_other_id']}"
        f"/transition?nuevo_estado=EN_CURSO"
    )
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "EN_CURSO"

    a = c.get(f"/api/v1/alerts/{seeded['alert_other_id']}")
    assert a.status_code == 200, a.text
    body = a.json()
    assert body["estado"] == "abierta", body
    assert body["resolved_at"] is None
    assert "[auto-resuelta" not in body["descripcion"]


# ----------------------------------------------------------------------------
# 3. GET /alerts default hides alerts on CERRADO dias
# ----------------------------------------------------------------------------

def test_list_alerts_default_hides_cerrado_dia_alerts(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [1])
    r = c.get("/api/v1/alerts")
    assert r.status_code == 200, r.text
    ids = {row["alert_id"] for row in r.json()}
    # On CERRADO dia: hidden.
    assert seeded["alert_on_cerrado_id"] not in ids
    # On EN_CURSO / VALIDADO dias: visible.
    assert seeded["alert_open_id"] in ids
    assert seeded["alert_notified_id"] in ids
    assert seeded["alert_other_id"] in ids


# ----------------------------------------------------------------------------
# 4. GET /alerts?incluir_cerradas=true returns CERRADO-dia alerts
# ----------------------------------------------------------------------------

def test_list_alerts_incluir_cerradas_returns_all(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [1])
    r = c.get("/api/v1/alerts?incluir_cerradas=true")
    assert r.status_code == 200, r.text
    ids = {row["alert_id"] for row in r.json()}
    assert seeded["alert_on_cerrado_id"] in ids
    assert seeded["alert_open_id"] in ids
    assert seeded["alert_notified_id"] in ids
    assert seeded["alert_other_id"] in ids
    assert seeded["alert_manual_no_dia_id"] in ids


# ----------------------------------------------------------------------------
# 5. manual alerts with dia_id=NULL are always visible
# ----------------------------------------------------------------------------

def test_list_alerts_manual_without_dia_always_visible(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [1])
    # default (incluir_cerradas=false)
    r1 = c.get("/api/v1/alerts")
    assert r1.status_code == 200
    ids1 = {row["alert_id"] for row in r1.json()}
    assert seeded["alert_manual_no_dia_id"] in ids1

    # explicit true
    r2 = c.get("/api/v1/alerts?incluir_cerradas=true")
    assert r2.status_code == 200
    ids2 = {row["alert_id"] for row in r2.json()}
    assert seeded["alert_manual_no_dia_id"] in ids2

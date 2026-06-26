"""CR-024 Part B2/B3 — campos nuevos del cliente (ventana, dias, prioridad).

Verifies persistence via PATCH, Pydantic validation of `dias_no_disponible`
and `prioridad`, and the visitas-futuras lookahead endpoint shape.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
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
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.visita import Visita
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
async def seeded(_engine):
    today = datetime.now(UTC).date()
    async with _engine() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        d = DiaOperativo(empresa_id=1, fecha=today + timedelta(days=2), estado="VALIDADO")
        d_far = DiaOperativo(empresa_id=1, fecha=today + timedelta(days=20), estado="BORRADOR")
        db.add_all([d, d_far])
        await db.flush()
        cli = Cliente(
            nombre="Reglado", rut="RUT-RG",
            es_vip=False, geocoding_status="pending", geocoding_attempts=0,
        )
        db.add(cli)
        await db.flush()
        v_close = Visita(dia_id=d.dia_id, empresa_id=1, orden=1,
                         cliente_id=cli.cliente_id, cliente_nombre="Reglado",
                         direccion="Cerca", estado="pendiente")
        v_far = Visita(dia_id=d_far.dia_id, empresa_id=1, orden=1,
                       cliente_id=cli.cliente_id, cliente_nombre="Reglado",
                       direccion="Lejos", estado="pendiente")
        db.add_all([v_close, v_far])
        # CR-027: link cliente <-> empresa is derived from visitas above.
        await db.commit()
        return {
            "engine_sm": _engine,
            "cliente_id": cli.cliente_id,
            "v_close_id": v_close.visita_id,
            "v_far_id": v_far.visita_id,
        }


def _override_admin():
    async def _stub() -> User:
        u = User(user_id=10, email="adm@td.cl", password_hash="x",
                 display_name="Adm", role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(seeded):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    def make() -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _override_admin()
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def test_patch_reglas_persisten(client_factory, seeded):
    c = client_factory()
    r = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}",
        json={
            "ventana_horaria_inicio": "09:00",
            "ventana_horaria_fin": "13:00",
            "dias_no_disponible": ["sat", "sun"],
            "prioridad": 1,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ventana_horaria_inicio"].startswith("09:00")
    assert body["ventana_horaria_fin"].startswith("13:00")
    assert body["dias_no_disponible"] == ["sat", "sun"]
    assert body["prioridad"] == 1

    # Second GET → values persisted.
    r2 = c.get(f"/api/v1/clientes/{seeded['cliente_id']}")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["prioridad"] == 1
    assert body2["dias_no_disponible"] == ["sat", "sun"]


def test_dias_no_disponible_validation(client_factory, seeded):
    """Codigo invalido (e.g. 'monday') → 422."""
    c = client_factory()
    r = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}",
        json={"dias_no_disponible": ["monday", "tuesday"]},
    )
    assert r.status_code == 422, r.text


def test_prioridad_validation(client_factory, seeded):
    """prioridad=0 o 6 → 422."""
    c = client_factory()
    r1 = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}", json={"prioridad": 0}
    )
    r2 = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}", json={"prioridad": 6}
    )
    assert r1.status_code == 422
    assert r2.status_code == 422


def test_visitas_futuras_default_7_days(client_factory, seeded):
    """default days=7 → solo v_close (today+2). v_far (today+20) NO aparece."""
    c = client_factory()
    r = c.get(f"/api/v1/clientes/{seeded['cliente_id']}/visitas-futuras")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dias_lookahead"] == 7
    assert body["total"] == 1
    assert body["items"][0]["visita_id"] == seeded["v_close_id"]


def test_visitas_futuras_extended_window(client_factory, seeded):
    """days=30 abarca ambas visitas."""
    c = client_factory()
    r = c.get(
        f"/api/v1/clientes/{seeded['cliente_id']}/visitas-futuras?days=30"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    ids = {x["visita_id"] for x in body["items"]}
    assert ids == {seeded["v_close_id"], seeded["v_far_id"]}

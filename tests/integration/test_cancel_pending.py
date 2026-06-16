"""CR-024 Part A2 — POST cancel-pending-visitas.

Verifies the 3 scope variants (all / today / next_n_days), the multi-tenant
filter, and the motivo formatting.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

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
async def seeded(_engine: async_sessionmaker[AsyncSession]) -> dict:
    """Seed: 2 empresas, 1 cliente served by both.

    Days:
      - day_today_e1   : today, EN_CURSO, empresa 1
      - day_today_e2   : today, EN_CURSO, empresa 2
      - day_t3_e1      : today+3, VALIDADO, empresa 1
      - day_t10_e1     : today+10, BORRADOR, empresa 1
      - day_cerrado_e1 : today-1, CERRADO, empresa 1

    Visitas: one pendiente per day, for cliente.
    """
    today = datetime.now(UTC).date()
    async with _engine() as db:
        db.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        await db.flush()

        days = {
            "today_e1": DiaOperativo(empresa_id=1, fecha=today, estado="EN_CURSO"),
            "today_e2": DiaOperativo(empresa_id=2, fecha=today, estado="EN_CURSO"),
            "t3_e1": DiaOperativo(empresa_id=1, fecha=today + timedelta(days=3), estado="VALIDADO"),
            "t10_e1": DiaOperativo(empresa_id=1, fecha=today + timedelta(days=10), estado="BORRADOR"),
            "cerrado_e1": DiaOperativo(empresa_id=1, fecha=today - timedelta(days=1), estado="CERRADO"),
        }
        for d in days.values():
            db.add(d)
        await db.flush()

        cli = Cliente(
            nombre="X", rut="RUT-X",
            es_vip=False, geocoding_status="pending", geocoding_attempts=0,
        )
        db.add(cli)
        await db.flush()

        visitas = {
            "today_e1": Visita(dia_id=days["today_e1"].dia_id, empresa_id=1, orden=1,
                               cliente_id=cli.cliente_id, cliente_nombre="X",
                               direccion="A", estado="pendiente"),
            "today_e2": Visita(dia_id=days["today_e2"].dia_id, empresa_id=2, orden=1,
                               cliente_id=cli.cliente_id, cliente_nombre="X",
                               direccion="B", estado="pendiente"),
            "t3_e1": Visita(dia_id=days["t3_e1"].dia_id, empresa_id=1, orden=1,
                            cliente_id=cli.cliente_id, cliente_nombre="X",
                            direccion="C", estado="pendiente"),
            "t10_e1": Visita(dia_id=days["t10_e1"].dia_id, empresa_id=1, orden=1,
                             cliente_id=cli.cliente_id, cliente_nombre="X",
                             direccion="D", estado="pendiente"),
            "cerrado_e1": Visita(dia_id=days["cerrado_e1"].dia_id, empresa_id=1, orden=1,
                                 cliente_id=cli.cliente_id, cliente_nombre="X",
                                 direccion="E", estado="pendiente"),
        }
        for v in visitas.values():
            db.add(v)
        # CR-027: no cliente_empresas table. The visitas above already
        # establish the link cliente <-> empresa via dias_operativos.
        await db.commit()

        return {
            "engine_sm": _engine,
            "cliente_id": cli.cliente_id,
            "days": {k: v.dia_id for k, v in days.items()},
            "visitas": {k: v.visita_id for k, v in visitas.items()},
        }


def _override_user(role: str, user_id: int, empresa_ids: list[int]):
    async def _stub() -> User:
        u = User(user_id=user_id, email=f"{role}@td.cl", password_hash="x",
                 display_name=role, role=role, activo=True)
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
        app.dependency_overrides[current_user] = _override_user(role, user_id, empresa_ids)
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def test_cancel_all_admin(client_factory, seeded):
    """Admin scope=all → cancels all 4 visitas active (excludes CERRADO)."""
    c = client_factory("falabella_admin", 10, [])
    r = c.post(
        f"/api/v1/clientes/{seeded['cliente_id']}/cancel-pending-visitas",
        json={"motivo": "Cliente de vacaciones", "scope": "all"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancelled_count"] == 4  # today_e1, today_e2, t3_e1, t10_e1
    assert seeded["visitas"]["cerrado_e1"] not in body["visita_ids"]


def test_cancel_today_admin(client_factory, seeded):
    """scope=today → only the 2 today visitas (in EN_CURSO days)."""
    c = client_factory("falabella_admin", 10, [])
    r = c.post(
        f"/api/v1/clientes/{seeded['cliente_id']}/cancel-pending-visitas",
        json={"motivo": "Hoy no", "scope": "today"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancelled_count"] == 2
    assert set(body["visita_ids"]) == {
        seeded["visitas"]["today_e1"], seeded["visitas"]["today_e2"]
    }


def test_cancel_next_n_days_requires_dias(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    r = c.post(
        f"/api/v1/clientes/{seeded['cliente_id']}/cancel-pending-visitas",
        json={"motivo": "X", "scope": "next_n_days"},
    )
    assert r.status_code == 400, r.text


def test_cancel_next_n_days_dias_5_admin(client_factory, seeded):
    """scope=next_n_days, dias=5 → today + t3 (not t10)."""
    c = client_factory("falabella_admin", 10, [])
    r = c.post(
        f"/api/v1/clientes/{seeded['cliente_id']}/cancel-pending-visitas",
        json={"motivo": "Semana off", "scope": "next_n_days", "dias": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancelled_count"] == 3
    assert set(body["visita_ids"]) == {
        seeded["visitas"]["today_e1"],
        seeded["visitas"]["today_e2"],
        seeded["visitas"]["t3_e1"],
    }


def test_cancel_manager_scoped(client_factory, seeded):
    """transport_manager empresa 1 cancela solo sus visitas, NO la de empresa 2."""
    c = client_factory("transport_manager", 20, [1])
    r = c.post(
        f"/api/v1/clientes/{seeded['cliente_id']}/cancel-pending-visitas",
        json={"motivo": "Vacaciones", "scope": "all"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert seeded["visitas"]["today_e2"] not in body["visita_ids"]
    assert seeded["visitas"]["today_e1"] in body["visita_ids"]


@pytest.mark.asyncio
async def test_cancelled_motivo_formatting(seeded):
    """La visita queda con estado=cancelado y motivo formateado."""
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_user("falabella_admin", 10, [])
    try:
        with TestClient(app) as c:
            r = c.post(
                f"/api/v1/clientes/{seeded['cliente_id']}/cancel-pending-visitas",
                json={"motivo": "De vacaciones hasta el 15", "scope": "today"},
            )
            assert r.status_code == 200
        async with sm() as db:
            v = (await db.execute(select(Visita).where(
                Visita.visita_id == seeded["visitas"]["today_e1"]
            ))).scalar_one()
            assert v.estado == "cancelado"
            assert v.motivo and v.motivo.startswith("Cancelado por cliente:")
            assert v.motivo_comentario == "Cancelado desde master cliente"
    finally:
        app.dependency_overrides.clear()

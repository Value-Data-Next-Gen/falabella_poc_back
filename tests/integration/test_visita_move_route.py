"""CR-028 Part A — POST /api/v1/operacion/visitas/{id}/move-route.

Verifies:
  * Happy path: visita moves to another ruta of the SAME dia. Origin neighbours
    shift down, destination neighbours shift up.
  * Default nuevo_orden = max(orden)+1 in destination.
  * Cross-dia ruta → 400.
  * Cross-tenant attempt → 403.
  * Same-ruta move → 400.
  * Audit row recorded.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date

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
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento
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
    async with _engine() as db:
        db.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        db.add_all([
            Driver(driver_id="DRV-1", empresa_id=1, nombre="D1", activo=True),
            Driver(driver_id="DRV-2", empresa_id=1, nombre="D2", activo=True),
            Driver(driver_id="DRV-3", empresa_id=2, nombre="D3", activo=True),
        ])
        await db.flush()
        d_e1 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="VALIDADO")
        d_e1_other = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 2), estado="VALIDADO")
        d_e2 = DiaOperativo(empresa_id=2, fecha=date(2026, 6, 1), estado="VALIDADO")
        db.add_all([d_e1, d_e1_other, d_e2])
        await db.flush()
        rA = Ruta(dia_id=d_e1.dia_id, driver_id="DRV-1", orden=1)
        rB = Ruta(dia_id=d_e1.dia_id, driver_id="DRV-2", orden=2)
        r_other_dia = Ruta(dia_id=d_e1_other.dia_id, driver_id="DRV-1", orden=1)
        r_e2 = Ruta(dia_id=d_e2.dia_id, driver_id="DRV-3", orden=1)
        db.add_all([rA, rB, r_other_dia, r_e2])
        await db.flush()
        # rA: 3 visitas, rB: 2 visitas, r_e2: 1 visita.
        rA_vs = [
            Visita(dia_id=d_e1.dia_id, empresa_id=1, ruta_id=rA.ruta_id, orden=i,
                   cliente_nombre=f"A{i}", direccion="a", estado="pendiente")
            for i in range(1, 4)
        ]
        rB_vs = [
            Visita(dia_id=d_e1.dia_id, empresa_id=1, ruta_id=rB.ruta_id, orden=i,
                   cliente_nombre=f"B{i}", direccion="b", estado="pendiente")
            for i in range(1, 3)
        ]
        v_e2 = Visita(dia_id=d_e2.dia_id, empresa_id=2, ruta_id=r_e2.ruta_id, orden=1,
                      cliente_nombre="E2-v", direccion="e2", estado="pendiente")
        db.add_all(rA_vs + rB_vs + [v_e2])
        admin = User(user_id=10, email="a@td.cl", password_hash="x",
                     display_name="A", role="falabella_admin", activo=True)
        mgr = User(user_id=20, email="m@td.cl", password_hash="x",
                   display_name="M", role="transport_manager", activo=True)
        db.add_all([admin, mgr])
        await db.flush()
        db.add(UserEmpresa(user_id=20, empresa_id=1))
        await db.commit()
        return {
            "engine_sm": _engine,
            "rA_id": rA.ruta_id, "rB_id": rB.ruta_id,
            "r_other_dia_id": r_other_dia.ruta_id,
            "r_e2_id": r_e2.ruta_id,
            "rA_vs": [v.visita_id for v in rA_vs],
            "rB_vs": [v.visita_id for v in rB_vs],
            "v_e2_id": v_e2.visita_id,
        }


def _override_user(role: str, uid: int, eids: list[int]):
    async def _stub() -> User:
        u = User(user_id=uid, email=f"{role}@td.cl", password_hash="x",
                 display_name=role, role=role, activo=True)
        u._empresa_ids = eids  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(seeded):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    def make(role: str = "falabella_admin", uid: int = 10, eids: list[int] | None = None) -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _override_user(role, uid, eids or [])
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


async def _ruta_ordens(sm, ruta_id: int) -> list[tuple[int, int]]:
    async with sm() as db:
        rows = (
            await db.execute(
                select(Visita).where(Visita.ruta_id == ruta_id).order_by(Visita.orden)
            )
        ).scalars().all()
        return [(v.visita_id, v.orden) for v in rows]


async def test_move_visita_appends_by_default(client_factory, seeded):
    c = client_factory()
    # Move rA[2] (orden=2) → rB, default nuevo_orden = 3.
    r = c.post(
        f"/api/v1/operacion/visitas/{seeded['rA_vs'][1]}/move-route",
        json={"nueva_ruta_id": seeded["rB_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ruta_id"] == seeded["rB_id"]
    assert body["orden"] == 3

    # rA neighbours shift: rA[0] keeps 1; rA[2] goes from 3 → 2.
    rA_after = await _ruta_ordens(seeded["engine_sm"], seeded["rA_id"])
    rA_map = dict(rA_after)
    assert rA_map[seeded["rA_vs"][0]] == 1
    assert rA_map[seeded["rA_vs"][2]] == 2
    assert seeded["rA_vs"][1] not in rA_map

    # rB: 3 visitas total.
    rB_after = await _ruta_ordens(seeded["engine_sm"], seeded["rB_id"])
    assert len(rB_after) == 3
    assert rB_after[-1] == (seeded["rA_vs"][1], 3)

    # Audit row.
    async with seeded["engine_sm"]() as db:
        rows = (
            await db.execute(
                select(VisitaEvento).where(
                    VisitaEvento.visita_id == seeded["rA_vs"][1],
                    VisitaEvento.tipo == "ruta_change",
                )
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_move_visita_inserts_at_specific_position(client_factory, seeded):
    c = client_factory()
    # Move rA[0] (orden=1) → rB at orden=1 → shifts rB[0]:1→2, rB[1]:2→3.
    r = c.post(
        f"/api/v1/operacion/visitas/{seeded['rA_vs'][0]}/move-route",
        json={"nueva_ruta_id": seeded["rB_id"], "nuevo_orden": 1},
    )
    assert r.status_code == 200, r.text
    rB_after = await _ruta_ordens(seeded["engine_sm"], seeded["rB_id"])
    assert rB_after[0] == (seeded["rA_vs"][0], 1)
    assert rB_after[1] == (seeded["rB_vs"][0], 2)
    assert rB_after[2] == (seeded["rB_vs"][1], 3)


async def test_move_visita_to_other_dia_400(client_factory, seeded):
    c = client_factory()
    r = c.post(
        f"/api/v1/operacion/visitas/{seeded['rA_vs'][0]}/move-route",
        json={"nueva_ruta_id": seeded["r_other_dia_id"]},
    )
    assert r.status_code == 400, r.text
    assert "otro día" in r.json()["detail"]


async def test_move_visita_cross_tenant_403(client_factory, seeded):
    # transport_manager scoped to empresa 1 cannot move empresa 2's visita.
    c = client_factory(role="transport_manager", uid=20, eids=[1])
    r = c.post(
        f"/api/v1/operacion/visitas/{seeded['v_e2_id']}/move-route",
        json={"nueva_ruta_id": seeded["rA_id"]},
    )
    assert r.status_code == 403, r.text


async def test_move_visita_same_ruta_400(client_factory, seeded):
    c = client_factory()
    r = c.post(
        f"/api/v1/operacion/visitas/{seeded['rA_vs'][0]}/move-route",
        json={"nueva_ruta_id": seeded["rA_id"]},
    )
    assert r.status_code == 400, r.text

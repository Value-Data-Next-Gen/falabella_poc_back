"""CR-028 Part A — PATCH /api/v1/operacion/visitas/{id}/orden.

Verifies:
  * Moving a visita forward shifts neighbours down by 1.
  * Moving a visita backward shifts neighbours up by 1.
  * No-op (same orden) still records an audit row marked noop.
  * 400 when the parent dia is CERRADO.
  * Each successful reorder appends one row to td.visita_eventos.
  * Cross-tenant attempt → 403.
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
        e1 = Empresa(empresa_id=1, nombre="E1", activo=True)
        e2 = Empresa(empresa_id=2, nombre="E2", activo=True)
        db.add_all([e1, e2])
        await db.flush()
        drv1 = Driver(driver_id="DRV-1", empresa_id=1, nombre="D1", activo=True)
        drv2 = Driver(driver_id="DRV-2", empresa_id=2, nombre="D2", activo=True)
        db.add_all([drv1, drv2])
        await db.flush()
        d1 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="VALIDADO")
        d2 = DiaOperativo(empresa_id=2, fecha=date(2026, 6, 1), estado="VALIDADO")
        db.add_all([d1, d2])
        await db.flush()
        r1 = Ruta(dia_id=d1.dia_id, driver_id="DRV-1", orden=1)
        r2 = Ruta(dia_id=d2.dia_id, driver_id="DRV-2", orden=1)
        db.add_all([r1, r2])
        await db.flush()
        # 4 visitas in r1, ordered 1..4.
        visitas = [
            Visita(
                dia_id=d1.dia_id, empresa_id=1, ruta_id=r1.ruta_id, orden=i,
                cliente_nombre=f"C{i}", direccion=f"addr {i}", estado="pendiente",
            )
            for i in range(1, 5)
        ]
        db.add_all(visitas)
        # 1 visita in empresa 2 to test cross-tenant.
        v_other = Visita(
            dia_id=d2.dia_id, empresa_id=2, ruta_id=r2.ruta_id, orden=1,
            cliente_nombre="C_other", direccion="otra", estado="pendiente",
        )
        db.add(v_other)
        admin = User(user_id=10, email="adm@td.cl", password_hash="x",
                     display_name="A", role="falabella_admin", activo=True)
        mgr = User(user_id=20, email="mgr@td.cl", password_hash="x",
                   display_name="M", role="transport_manager", activo=True)
        db.add_all([admin, mgr])
        await db.flush()
        db.add(UserEmpresa(user_id=20, empresa_id=1))
        await db.commit()
        return {
            "engine_sm": _engine,
            "d1_id": d1.dia_id,
            "r1_id": r1.ruta_id,
            "v_ids": [v.visita_id for v in visitas],
            "v_other_id": v_other.visita_id,
        }


def _override_user(role: str, user_id: int, empresa_ids: list[int]):
    async def _stub() -> User:
        u = User(user_id=user_id, email=f"{role}@td.cl", password_hash="x",
                 display_name=role, role=role, activo=True)
        u._empresa_ids = empresa_ids  # type: ignore[attr-defined]
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


async def _orden_map(sm, ruta_id: int) -> dict[int, int]:
    async with sm() as db:
        rows = (await db.execute(select(Visita).where(Visita.ruta_id == ruta_id))).scalars().all()
        return {v.visita_id: v.orden for v in rows}


async def _audit_count(sm, visita_id: int) -> int:
    async with sm() as db:
        rows = (
            await db.execute(select(VisitaEvento).where(VisitaEvento.visita_id == visita_id))
        ).scalars().all()
        return len(rows)


async def test_reorder_forward_shifts_neighbours(client_factory, seeded):
    c = client_factory()
    v_ids = seeded["v_ids"]  # [v1, v2, v3, v4] @ orden 1,2,3,4
    # Move v1 to position 3 → v2,v3 shift down; v4 stays.
    r = c.patch(
        f"/api/v1/operacion/visitas/{v_ids[0]}/orden",
        json={"nuevo_orden": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["orden"] == 3
    mp = await _orden_map(seeded["engine_sm"], seeded["r1_id"])
    assert mp[v_ids[0]] == 3
    assert mp[v_ids[1]] == 1  # was 2 → 1
    assert mp[v_ids[2]] == 2  # was 3 → 2
    assert mp[v_ids[3]] == 4  # untouched
    assert await _audit_count(seeded["engine_sm"], v_ids[0]) == 1


async def test_reorder_backward_shifts_neighbours(client_factory, seeded):
    c = client_factory()
    v_ids = seeded["v_ids"]
    # Move v4 (orden=4) to position 1 → v1,v2,v3 shift up.
    r = c.patch(
        f"/api/v1/operacion/visitas/{v_ids[3]}/orden",
        json={"nuevo_orden": 1},
    )
    assert r.status_code == 200, r.text
    mp = await _orden_map(seeded["engine_sm"], seeded["r1_id"])
    assert mp[v_ids[3]] == 1
    assert mp[v_ids[0]] == 2
    assert mp[v_ids[1]] == 3
    assert mp[v_ids[2]] == 4


async def test_reorder_noop_records_audit(client_factory, seeded):
    c = client_factory()
    v_ids = seeded["v_ids"]
    r = c.patch(
        f"/api/v1/operacion/visitas/{v_ids[0]}/orden",
        json={"nuevo_orden": 1},  # already 1
    )
    assert r.status_code == 200, r.text
    assert await _audit_count(seeded["engine_sm"], v_ids[0]) == 1


async def test_reorder_blocked_when_dia_cerrado(client_factory, seeded):
    # Manually flip dia to CERRADO.
    async with seeded["engine_sm"]() as db:
        d = (
            await db.execute(select(DiaOperativo).where(DiaOperativo.dia_id == seeded["d1_id"]))
        ).scalar_one()
        d.estado = "CERRADO"
        await db.commit()
    c = client_factory()
    r = c.patch(
        f"/api/v1/operacion/visitas/{seeded['v_ids'][0]}/orden",
        json={"nuevo_orden": 2},
    )
    assert r.status_code == 400, r.text


async def test_reorder_cross_tenant_403(client_factory, seeded):
    # transport_manager scoped to empresa 1 cannot reorder empresa 2's visita.
    c = client_factory(role="transport_manager", uid=20, eids=[1])
    r = c.patch(
        f"/api/v1/operacion/visitas/{seeded['v_other_id']}/orden",
        json={"nuevo_orden": 2},
    )
    assert r.status_code == 403, r.text

"""CR-028 Part A — POST /api/v1/operacion/rutas/{ruta_id}/promote-vips.

Verifies:
  * VIPs bubble to the top, non-VIPs preserve their relative order.
  * Completed/cancelled visitas keep stable trailing position.
  * Empty pending → vips_promoted=0, visitas_reordered=0.
  * 404 when ruta does not exist.
  * Each moved VIP gets an audit row.
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
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        db.add(Driver(driver_id="DRV-1", empresa_id=1, nombre="D", activo=True))
        await db.flush()
        d = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        db.add(d)
        await db.flush()
        r = Ruta(dia_id=d.dia_id, driver_id="DRV-1", orden=1)
        r_empty = Ruta(dia_id=d.dia_id, driver_id="DRV-1", orden=2)
        db.add_all([r, r_empty])
        await db.flush()

        # Mixed bag in r:
        #   orden=1: non-VIP pendiente
        #   orden=2: VIP pendiente   <-- should jump to head
        #   orden=3: entregado (stays at the back)
        #   orden=4: non-VIP pendiente
        #   orden=5: VIP en_camino   <-- should be second VIP
        #   orden=6: cancelado (stays at the back)
        v1 = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=1,
                    cliente_nombre="N1", direccion="a", estado="pendiente", es_vip=False)
        v2 = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=2,
                    cliente_nombre="V1", direccion="a", estado="pendiente", es_vip=True)
        v3 = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=3,
                    cliente_nombre="Done", direccion="a", estado="entregado", es_vip=False)
        v4 = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=4,
                    cliente_nombre="N2", direccion="a", estado="pendiente", es_vip=False)
        v5 = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=5,
                    cliente_nombre="V2", direccion="a", estado="en_camino", es_vip=True)
        v6 = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=6,
                    cliente_nombre="Cnx", direccion="a", estado="cancelado", es_vip=False)
        db.add_all([v1, v2, v3, v4, v5, v6])
        await db.commit()
        return {
            "engine_sm": _engine,
            "r_id": r.ruta_id,
            "r_empty_id": r_empty.ruta_id,
            "v1_id": v1.visita_id, "v2_id": v2.visita_id, "v3_id": v3.visita_id,
            "v4_id": v4.visita_id, "v5_id": v5.visita_id, "v6_id": v6.visita_id,
        }


def _override_admin():
    async def _stub() -> User:
        u = User(user_id=10, email="a@td.cl", password_hash="x",
                 display_name="A", role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client(seeded):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


async def test_promote_vips_reorders_pending(client, seeded):
    r = client.post(f"/api/v1/operacion/rutas/{seeded['r_id']}/promote-vips")
    assert r.status_code == 200, r.text
    body = r.json()
    # 2 VIPs both moved.
    assert body["vips_promoted"] == 2
    # All 4 pending visitas have new orden values.
    assert body["visitas_reordered"] >= 2

    # Verify the final ordering:
    #   pending (VIPs first):   v2(VIP), v5(VIP), v1, v4
    #   completed/cancelled:    v3, v6  (appended)
    async with seeded["engine_sm"]() as db:
        rows = (
            await db.execute(
                select(Visita).where(Visita.ruta_id == seeded["r_id"]).order_by(Visita.orden)
            )
        ).scalars().all()
    ordered_ids = [v.visita_id for v in rows]
    assert ordered_ids[0] == seeded["v2_id"]  # VIP first
    assert ordered_ids[1] == seeded["v5_id"]  # VIP second
    # next 2 are pending non-VIPs in their original order
    assert set(ordered_ids[2:4]) == {seeded["v1_id"], seeded["v4_id"]}
    assert ordered_ids[2] == seeded["v1_id"]
    assert ordered_ids[3] == seeded["v4_id"]
    # last 2 are completed/cancelled
    assert set(ordered_ids[4:6]) == {seeded["v3_id"], seeded["v6_id"]}


async def test_promote_vips_creates_audit_rows(client, seeded):
    client.post(f"/api/v1/operacion/rutas/{seeded['r_id']}/promote-vips")
    async with seeded["engine_sm"]() as db:
        v2_events = (
            await db.execute(
                select(VisitaEvento).where(
                    VisitaEvento.visita_id == seeded["v2_id"],
                    VisitaEvento.tipo == "promoted_vip",
                )
            )
        ).scalars().all()
    assert len(v2_events) == 1


async def test_promote_vips_empty_ruta(client, seeded):
    r = client.post(f"/api/v1/operacion/rutas/{seeded['r_empty_id']}/promote-vips")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vips_promoted"] == 0
    assert body["visitas_reordered"] == 0


async def test_promote_vips_ruta_404(client):
    r = client.post("/api/v1/operacion/rutas/999999/promote-vips")
    assert r.status_code == 404, r.text

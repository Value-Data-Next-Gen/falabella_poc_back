"""vip_deadline_job fires only for VIP visitas whose ETA is APPROACHING.

Regression test for the missing lower bound: the query used to match
`eta_estimada <= sim_now + deadline` with no `>= sim_now`, so a VIP visita
already hours overdue kept firing "deadline próximo". Overdue VIPs are covered
by eta_breach instead.

Modelled on test_eta_breach_escalation.py (monkeypatch get_sessionmaker +
no-op dispatch).
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

from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.sim_clock import SimClock
from app.db.models.visita import Visita


@pytest_asyncio.fixture
async def sm_engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
async def seeded(sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    async with sm_engine() as db:
        db.add(SimClock(id=1, sim_now=sim_now))
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        day = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        db.add(day)
        await db.flush()
        # VIP approaching (within the 30-min deadline window) → should fire.
        v_soon = Visita(
            dia_id=day.dia_id, empresa_id=1, orden=1,
            cliente_nombre="VIP-soon", direccion="A", estado="pendiente",
            eta_estimada=sim_now + timedelta(minutes=10), folio_cliente="S", es_vip=1,
        )
        # VIP already 1h overdue → should NOT fire (covered by eta_breach).
        v_overdue = Visita(
            dia_id=day.dia_id, empresa_id=1, orden=2,
            cliente_nombre="VIP-overdue", direccion="B", estado="pendiente",
            eta_estimada=sim_now - timedelta(minutes=60), folio_cliente="O", es_vip=1,
        )
        db.add_all([v_soon, v_overdue])
        await db.commit()
        return {"sm_engine": sm_engine, "soon": v_soon.visita_id, "overdue": v_overdue.visita_id}


@pytest.mark.asyncio
async def test_vip_deadline_fires_only_for_approaching(monkeypatch, seeded):
    from app.jobs import alerts as alerts_module
    monkeypatch.setattr(alerts_module, "get_sessionmaker", lambda: seeded["sm_engine"])

    async def _noop_dispatch(*args, **kwargs):
        class R:
            sent = 0
            skipped = 0
        return R()

    monkeypatch.setattr(alerts_module, "dispatch_alert", _noop_dispatch)

    await alerts_module.vip_deadline_job()

    async with seeded["sm_engine"]() as db:
        rows = (await db.execute(
            select(Alert).where(Alert.tipo == "vip_deadline")
        )).scalars().all()
        visita_ids = {a.visita_id for a in rows}
    assert seeded["soon"] in visita_ids, "approaching VIP should get a vip_deadline alert"
    assert seeded["overdue"] not in visita_ids, "overdue VIP must NOT get 'deadline próximo'"

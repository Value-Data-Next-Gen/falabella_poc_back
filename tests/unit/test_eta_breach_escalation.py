"""CR-024 Part B4 — eta_breach_job escalates severity for VIP / prioridad<=2.

The cron itself uses an internal sessionmaker. We verify the escalation logic
by exercising the same selection + branching path: feed the job a SimClock,
EN_CURSO day, and 2 visitas (one VIP, one normal). After the job runs, the
generated alerts should have severity="critica" for the VIP visita and
"alta" for the normal one.

We override `get_sessionmaker` to return our in-memory sessionmaker so the job
operates on the test DB.
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
from app.db.models.cliente import Cliente
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
    breach_eta = sim_now - timedelta(minutes=60)  # 1h late (way beyond grace)

    async with sm_engine() as db:
        db.add(SimClock(id=1, sim_now=sim_now))
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        day = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        db.add(day)
        await db.flush()
        cli_vip = Cliente(
            nombre="VIP-Cli", rut="RUT-VIP",
            es_vip=True, geocoding_status="pending", geocoding_attempts=0,
        )
        cli_prio2 = Cliente(
            nombre="Prio2-Cli", rut="RUT-P2",
            es_vip=False, prioridad=2,
            geocoding_status="pending", geocoding_attempts=0,
        )
        cli_normal = Cliente(
            nombre="Normal-Cli", rut="RUT-N",
            es_vip=False, prioridad=4,
            geocoding_status="pending", geocoding_attempts=0,
        )
        db.add_all([cli_vip, cli_prio2, cli_normal])
        await db.flush()

        v_vip = Visita(
            dia_id=day.dia_id, empresa_id=1, orden=1, cliente_id=cli_vip.cliente_id,
            cliente_nombre="VIP-Cli", direccion="A", estado="pendiente",
            eta_estimada=breach_eta, folio_cliente="V1", es_vip=1,
        )
        v_prio2 = Visita(
            dia_id=day.dia_id, empresa_id=1, orden=2, cliente_id=cli_prio2.cliente_id,
            cliente_nombre="Prio2-Cli", direccion="B", estado="pendiente",
            eta_estimada=breach_eta, folio_cliente="V2", es_vip=0,
        )
        v_normal = Visita(
            dia_id=day.dia_id, empresa_id=1, orden=3, cliente_id=cli_normal.cliente_id,
            cliente_nombre="Normal-Cli", direccion="C", estado="pendiente",
            eta_estimada=breach_eta, folio_cliente="V3", es_vip=0,
        )
        db.add_all([v_vip, v_prio2, v_normal])
        await db.commit()
        return {
            "sm_engine": sm_engine,
            "v_vip_id": v_vip.visita_id,
            "v_prio2_id": v_prio2.visita_id,
            "v_normal_id": v_normal.visita_id,
        }


@pytest.mark.asyncio
async def test_eta_breach_escalates_vip_and_prio(monkeypatch, seeded):
    """Run eta_breach_job, then check generated alerts.

    The dispatcher is monkeypatched into a no-op so the job doesn't try to
    notify recipients (we only care about severity of the persisted alert).
    """
    from app.jobs import alerts as alerts_module
    monkeypatch.setattr(alerts_module, "get_sessionmaker", lambda: seeded["sm_engine"])

    async def _noop_dispatch(*args, **kwargs):
        class R:
            sent = 0
            skipped = 0
        return R()

    monkeypatch.setattr(alerts_module, "dispatch_alert", _noop_dispatch)

    await alerts_module.eta_breach_job()

    async with seeded["sm_engine"]() as db:
        rows = (await db.execute(
            select(Alert).where(Alert.tipo == "eta_breach")
        )).scalars().all()
        by_visita = {a.visita_id: a for a in rows}
        # 3 alerts expected (one per visita).
        assert len(by_visita) == 3
        assert by_visita[seeded["v_vip_id"]].severity == "critica"
        assert by_visita[seeded["v_prio2_id"]].severity == "critica"
        assert by_visita[seeded["v_normal_id"]].severity == "alta"

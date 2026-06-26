"""CR-030 — sim auto-progression cron tests.

Five cases:
  1. visita with sim_now > eta + grace flips to entregado (forced 100% success).
  2. visita with sim_now < eta is NOT touched.
  3. sim_clock.running=False short-circuits — nothing changes.
  4. VIP success rate is statistically higher than non-VIP (100 visitas, fixed seed).
  5. audit row appended with tipo='estado_change' and user_id=None.

Strategy: same as test_eta_breach_escalation — monkeypatch
`app.jobs.sim_progression.get_sessionmaker` to return our in-memory
sessionmaker so the job runs against the test DB.
"""
from __future__ import annotations

import json
import os
import random
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
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.sim_clock import SimClock
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento


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


async def _seed_basic(
    sm: async_sessionmaker[AsyncSession],
    *,
    sim_now: datetime,
    running: bool,
    eta_offsets_min: list[int],
    vip_flags: list[bool] | None = None,
) -> dict:
    """Seed an empresa + EN_CURSO dia + N visitas with eta = sim_now + offset.

    Negative offset = ETA is in the past (eligible for progression).
    `vip_flags[i]` toggles `visita.es_vip` (also creates a VIP cliente row).
    """
    vip_flags = vip_flags or [False] * len(eta_offsets_min)
    assert len(vip_flags) == len(eta_offsets_min)

    visita_ids: list[int] = []
    async with sm() as db:
        db.add(SimClock(id=1, sim_now=sim_now, speed=1.0, running=running))
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        day = DiaOperativo(empresa_id=1, fecha=sim_now.date(), estado="EN_CURSO")
        db.add(day)
        await db.flush()

        for i, (offset, is_vip) in enumerate(zip(eta_offsets_min, vip_flags, strict=True)):
            cli = Cliente(
                nombre=f"Cli-{i}", rut=f"RUT-{i}",
                es_vip=bool(is_vip),
                geocoding_status="pending", geocoding_attempts=0,
            )
            db.add(cli)
            await db.flush()
            v = Visita(
                dia_id=day.dia_id, empresa_id=1, orden=i + 1,
                cliente_id=cli.cliente_id,
                cliente_nombre=cli.nombre, direccion="X",
                estado="pendiente",
                eta_estimada=sim_now + timedelta(minutes=offset),
                folio_cliente=f"F{i}",
                es_vip=1 if is_vip else 0,
            )
            db.add(v)
            await db.flush()
            visita_ids.append(v.visita_id)

        await db.commit()
    return {"day_id": day.dia_id, "visita_ids": visita_ids}


# ----------------------------------------------------------------------------
# 1. Past-ETA visita flips to entregado (forced 100% success via rng).
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_past_eta_visita_flips_to_entregado(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=True,
        eta_offsets_min=[-30],  # 30 min in the past, well beyond grace=2.
        vip_flags=[False],
    )
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    # Force 100% success: rng.random() always returns 0.0 → < success_rate.
    class _AlwaysSuccess(random.Random):
        def random(self) -> float: return 0.0
        def randint(self, a, b): return a
        def choices(self, *args, **kwargs): return ["SIN MORADORES"]
    counts = await sp.auto_progression_job(rng=_AlwaysSuccess())

    assert counts == {"scanned": 1, "delivered": 1, "failed": 0}
    async with sm_engine() as db:
        v = (await db.execute(
            select(Visita).where(Visita.visita_id == seeded["visita_ids"][0])
        )).scalar_one()
        assert v.estado == "entregado"
        # SQLite strips tzinfo on read; compare on the naive replacement.
        assert v.completada_at.replace(tzinfo=UTC) == sim_now
        assert v.llegada_at is not None
        assert v.llegada_at.replace(tzinfo=UTC) < sim_now


# ----------------------------------------------------------------------------
# 2. Future-ETA visita is NOT touched.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_future_eta_visita_untouched(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=True,
        eta_offsets_min=[+30],  # 30 min in the future.
    )
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    counts = await sp.auto_progression_job()
    assert counts == {"scanned": 0, "delivered": 0, "failed": 0}

    async with sm_engine() as db:
        v = (await db.execute(
            select(Visita).where(Visita.visita_id == seeded["visita_ids"][0])
        )).scalar_one()
        assert v.estado == "pendiente"
        assert v.completada_at is None


# ----------------------------------------------------------------------------
# 3. clock.running=False → no work, even with eligible visitas.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clock_not_running_short_circuits(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=False,
        eta_offsets_min=[-30],  # would be eligible if clock were running.
    )
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    counts = await sp.auto_progression_job()
    assert counts == {"scanned": 0, "delivered": 0, "failed": 0}

    async with sm_engine() as db:
        v = (await db.execute(
            select(Visita).where(Visita.visita_id == seeded["visita_ids"][0])
        )).scalar_one()
        assert v.estado == "pendiente"


# ----------------------------------------------------------------------------
# 4. VIP rate >= default rate (statistical).
#
# 100 VIPs (rate 0.99) vs 100 non-VIP (rate 0.92). With a fixed seed we expect
# VIP failures < non-VIP failures by a comfortable margin.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vip_success_rate_higher_than_default(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n_each = 100
    offsets = [-30] * (n_each * 2)
    vip_flags = [True] * n_each + [False] * n_each
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=True,
        eta_offsets_min=offsets, vip_flags=vip_flags,
    )
    from app.core import config as cfg_module
    # Make sure max_per_tick lets the whole batch through.
    monkeypatch.setattr(cfg_module.settings, "sim_progression_max_per_tick", 1000)
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    await sp.auto_progression_job(rng=random.Random(42))

    async with sm_engine() as db:
        vip_ids = set(seeded["visita_ids"][:n_each])
        rows = (await db.execute(
            select(Visita).where(Visita.dia_id == seeded["day_id"])
        )).scalars().all()
        vip_failed = sum(1 for v in rows if v.visita_id in vip_ids and v.estado == "no_entregado")
        non_vip_failed = sum(1 for v in rows if v.visita_id not in vip_ids and v.estado == "no_entregado")

        # Expected ~1 VIP fail vs ~8 non-VIP fails. We assert strict ordering
        # with a small slack so a noisy RNG draw doesn't flake the test.
        assert vip_failed < non_vip_failed, (
            f"VIP failed={vip_failed}, non-VIP failed={non_vip_failed} — "
            "expected VIP < non-VIP"
        )
        # All processed: every visita ended up in a terminal state.
        terminals = {v.estado for v in rows}
        assert terminals <= {"entregado", "no_entregado"}


# ----------------------------------------------------------------------------
# 5. Audit row appended with tipo='estado_change' and user_id=None.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_log_created_with_no_user(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=True,
        eta_offsets_min=[-30, -25],
    )
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    await sp.auto_progression_job(rng=random.Random(7))

    async with sm_engine() as db:
        eventos = (await db.execute(
            select(VisitaEvento).where(VisitaEvento.tipo == "estado_change")
        )).scalars().all()
        assert len(eventos) == 2
        for ev in eventos:
            assert ev.user_id is None
            assert ev.visita_id in seeded["visita_ids"]
            assert ev.payload_json is not None
            payload = json.loads(ev.payload_json)
            assert payload["old"] == "pendiente"
            assert payload["new"] in {"entregado", "no_entregado"}
            assert payload["source"] == "sim_progression"
            assert "sim_now" in payload


# ----------------------------------------------------------------------------
# 6. Auto-close: a dia with zero pending visitas after progression is CERRADO.
# ----------------------------------------------------------------------------

class _AlwaysSuccess(random.Random):
    def random(self) -> float: return 0.0
    def randint(self, a, b): return a
    def choices(self, *args, **kwargs): return ["SIN MORADORES"]


@pytest.mark.asyncio
async def test_dia_auto_closes_when_all_visitas_terminal(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=True, eta_offsets_min=[-30, -20],
    )
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    await sp.auto_progression_job(rng=_AlwaysSuccess())

    async with sm_engine() as db:
        dia = (await db.execute(
            select(DiaOperativo).where(DiaOperativo.dia_id == seeded["day_id"])
        )).scalar_one()
        assert dia.estado == "CERRADO", "dia with 0 pending visitas should auto-close"
        # sqlite drops tzinfo on read; normalize before comparing.
        assert dia.cerrado_at is not None
        assert dia.cerrado_at.replace(tzinfo=UTC) == sim_now


@pytest.mark.asyncio
async def test_dia_stays_open_while_a_visita_is_still_pending(monkeypatch, sm_engine):
    sim_now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    seeded = await _seed_basic(
        sm_engine, sim_now=sim_now, running=True,
        eta_offsets_min=[-30, +120],  # one past (progresses), one future (stays pending)
    )
    from app.jobs import sim_progression as sp
    monkeypatch.setattr(sp, "get_sessionmaker", lambda: sm_engine)

    await sp.auto_progression_job(rng=_AlwaysSuccess())

    async with sm_engine() as db:
        dia = (await db.execute(
            select(DiaOperativo).where(DiaOperativo.dia_id == seeded["day_id"])
        )).scalar_one()
        assert dia.estado == "EN_CURSO", "dia must stay open while a visita is pending"

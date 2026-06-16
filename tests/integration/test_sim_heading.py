"""CR-029 — verify driver_position.heading is non-zero between distinct visitas.

Three scenarios:

  1. Cold-start, sim_now < etas[0]: heading should be seeded from
     visita[0] → visita[1] (not 0). Previous bug: parked-at-visita[0] with
     prev_heading=0 left heading=0.

  2. Mid-leg between two visitas in DIFFERENT comunas: heading reflects the
     leg bearing, ≠ 0 and consistent with sign of Δlat/Δlon.

  3. Mid-leg between two visitas with IDENTICAL coords (degenerate): heading
     keeps prev_heading (no NaN, no snap to 0).

Implementation note: SQLite (used here for isolation) drops tzinfo from
DateTime(timezone=True) columns on roundtrip. The production code uses
`datetime.now(UTC)` (aware) and subtracts `clock.last_tick_at`, which would
raise TypeError against a naive value. We monkeypatch `datetime` in `app.api.v1.sim`
to a class returning naive UTC, matching what SQLite gives us. Production
against Azure SQL keeps the aware semantics.
"""
from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.driver_position import DriverPosition
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def _patch_sim_naive(monkeypatch, fake_now: datetime) -> None:
    """Force `app.api.v1.sim.datetime.now(...)` to return naive UTC.

    Required because SQLite strips tzinfo on `DateTime(timezone=True)` columns;
    subtracting aware - naive raises TypeError. We replace the datetime symbol
    inside the sim module with a Faux class that returns naive `fake_now` from
    `now()`. Other datetime APIs (timedelta, etc.) still work because we keep
    the original class behind it for non-now calls.
    """
    import app.api.v1.sim as sim_mod
    real_dt = sim_mod.datetime
    naive_now = fake_now.replace(tzinfo=None)

    class _FauxDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            return naive_now

    monkeypatch.setattr(sim_mod, "datetime", _FauxDateTime)


@pytest_asyncio.fixture(scope="function")
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sessionmaker
    await engine.dispose()


def _make_visita(dia_id, empresa_id, ruta_id, orden, lat, lon, eta):
    return Visita(
        dia_id=dia_id, empresa_id=empresa_id, ruta_id=ruta_id, orden=orden,
        cliente_nombre=f"C{orden}", direccion=f"Av {orden}",
        comuna="X", lat=lat, lon=lon, estado="pendiente",
        eta_estimada=eta,
    )


async def _seed(sessionmaker, sim_now: datetime, *, identical_coords: bool = False):
    """Build the minimal scenario: 1 empresa, 1 dia (EN_CURSO), 1 ruta, 3 visitas.

    visita[0] at La Cisterna, visita[1] at Providencia, visita[2] at Las Condes.
    All ETAs in the future relative to sim_now → tests the "pre-shift" branch.

    Datetimes seeded NAIVE because SQLite drops tzinfo anyway; this keeps the
    test consistent with what sim.py will read back from `clock.last_tick_at`.
    """
    sim_now = sim_now.replace(tzinfo=None)
    async with sessionmaker() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        db.add(Vehicle(vehicle_id=1, empresa_id=1, nombre="V", plate="ABC11", activo=True))
        db.add(Driver(driver_id="DRV-01001", empresa_id=1, nombre="D", activo=True))
        await db.flush()
        dia = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        db.add(dia)
        await db.flush()
        ruta = Ruta(dia_id=dia.dia_id, driver_id="DRV-01001", vehicle_id=1, folio="R", orden=1)
        db.add(ruta)
        await db.flush()

        if identical_coords:
            coords = [(-33.53, -70.66), (-33.53, -70.66), (-33.53, -70.66)]
        else:
            coords = [
                (-33.53, -70.66),  # La Cisterna
                (-33.43, -70.60),  # Providencia (NE of LC)
                (-33.42, -70.55),  # Las Condes (further NE)
            ]
        base_eta = sim_now + timedelta(hours=2)
        for i, (la, lo) in enumerate(coords):
            db.add(
                _make_visita(
                    dia.dia_id, 1, ruta.ruta_id, i + 1, la, lo,
                    base_eta + timedelta(hours=i),
                )
            )

        db.add(SimClock(
            id=1, sim_now=sim_now, speed=1.0, running=True,
            last_tick_at=sim_now - timedelta(seconds=2),
        ))
        await db.commit()
        return ruta.ruta_id


def _override_user_dep():
    async def _stub() -> User:
        u = User(
            user_id=1, email="a@td.cl", password_hash="x", display_name="A",
            role="falabella_admin", activo=True,
        )
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(_engine):
    sessionmaker = _engine

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_user_dep()
    yield TestClient(app), sessionmaker
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_start_seeds_heading_from_visita0_to_visita1(_engine, monkeypatch):
    """When sim_now < etas[0] and no driver_position row exists, heading should
    be the bearing from visita[0] → visita[1], NOT zero.
    """
    sim_now = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)  # before earliest eta (>=10:00).
    await _seed(_engine, sim_now)
    _patch_sim_naive(monkeypatch, sim_now)

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with _engine() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_user_dep()
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/sim/tick")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["drivers_moved"] == 1

        async with _engine() as session:
            pos = (
                await session.execute(select(DriverPosition))
            ).scalar_one()
            # visita[0] LC (-33.53, -70.66) → visita[1] PROV (-33.43, -70.60).
            # dy = -33.43 - (-33.53) = +0.10 (north), dx = -70.60 - (-70.66) = +0.06 (east).
            # heading = atan2(dx, dy) in degrees → ~ atan2(0.06, 0.10) ≈ 30.96°.
            assert pos.heading is not None
            assert pos.heading != 0.0
            expected = (math.degrees(math.atan2(0.06, 0.10)) + 360) % 360
            assert abs(pos.heading - expected) < 1.0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mid_leg_heading_reflects_leg_bearing(_engine, monkeypatch):
    """When sim_now lands between two distinct visitas, heading equals the leg
    bearing (atan2(Δlon, Δlat)).
    """
    # We want sim_now between etas[0] and etas[1]. `_seed` builds ETAs at
    # `seed_origin + 2h + i*1h`. To land sim_now at 10:30 between eta[0]=10:00
    # and eta[1]=11:00, seed with origin=8:00 and overwrite the SimClock.sim_now.
    seed_origin = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    # → etas = 10:00, 11:00, 12:00
    sim_now = datetime(2026, 6, 1, 10, 30, tzinfo=UTC)
    await _seed(_engine, seed_origin)
    async with _engine() as session:
        clock = (await session.execute(select(SimClock))).scalar_one()
        clock.sim_now = sim_now.replace(tzinfo=None)
        clock.last_tick_at = (sim_now - timedelta(seconds=2)).replace(tzinfo=None)
        await session.commit()
    # `_FauxDateTime.now()` returns `sim_now` (naive) which is +2s past
    # last_tick_at → real_elapsed=2s, sim advances slightly, drivers move.
    _patch_sim_naive(monkeypatch, sim_now)

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with _engine() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_user_dep()
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/sim/tick")
            assert r.status_code == 200, r.text
        async with _engine() as session:
            pos = (await session.execute(select(DriverPosition))).scalar_one()
            # leg 0 → 1: same Δ as cold-start.
            expected = (math.degrees(math.atan2(0.06, 0.10)) + 360) % 360
            assert pos.heading is not None
            assert abs(pos.heading - expected) < 1.0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_identical_coords_preserves_prev_heading(_engine, monkeypatch):
    """Degenerate ruta where all visitas share lat/lon. Cold start with
    no prev_heading should NOT crash; heading remains 0 (no real movement)
    AFTER the seed walk-forward fails to find a distinct visita.
    """
    sim_now = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    await _seed(_engine, sim_now, identical_coords=True)
    _patch_sim_naive(monkeypatch, sim_now)

    # Pre-seed a driver_position with a non-zero heading to assert it survives.
    async with _engine() as session:
        session.add(DriverPosition(
            driver_id="DRV-01001", lat=-33.53, lon=-70.66, heading=123.0,
            speed=0, visita_id=None,
        ))
        await session.commit()

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with _engine() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_user_dep()
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/sim/tick")
            assert r.status_code == 200, r.text
        async with _engine() as session:
            pos = (await session.execute(select(DriverPosition))).scalar_one()
            # No distinct visita → heading kept at prev (123.0).
            assert pos.heading == 123.0
    finally:
        app.dependency_overrides.clear()

"""Simulation clock + driver auto-advancement."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user, require_admin
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver_position import DriverPosition
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/sim", tags=["sim"])


class ClockOut(BaseModel):
    sim_now: datetime
    speed: float
    running: bool
    last_tick_at: datetime


class ClockUpdate(BaseModel):
    sim_now: datetime | None = None
    speed: float | None = Field(default=None, ge=0, le=600)
    running: bool | None = None


async def _get_clock(db: AsyncSession, *, for_update: bool = False) -> SimClock:
    stmt = select(SimClock).where(SimClock.id == 1)
    if for_update:
        # Row lock (MSSQL: WITH (UPDLOCK)) so concurrent /tick calls serialize:
        # without it two ticks read the same last_tick_at, both compute
        # real_elapsed from the same base, and double-advance sim_now.
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    clock = result.scalar_one_or_none()
    if not clock:
        clock = SimClock(id=1, sim_now=datetime.now(UTC), speed=1.0, running=False)
        db.add(clock)
        await db.commit()
        await db.refresh(clock)
    return clock


@router.get("/clock", operation_id="getSimClock", response_model=ClockOut)
async def get_clock(_user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> ClockOut:
    clock = await _get_clock(db)
    return ClockOut(sim_now=clock.sim_now, speed=clock.speed, running=clock.running, last_tick_at=clock.last_tick_at)


@router.patch("/clock", operation_id="updateSimClock", response_model=ClockOut, dependencies=[Depends(require_admin())])
async def update_clock(body: ClockUpdate, db: AsyncSession = Depends(get_db)) -> ClockOut:
    clock = await _get_clock(db)
    if body.sim_now is not None:
        clock.sim_now = body.sim_now
    if body.speed is not None:
        clock.speed = body.speed
    if body.running is not None:
        clock.running = body.running
    clock.last_tick_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(clock)
    return ClockOut(sim_now=clock.sim_now, speed=clock.speed, running=clock.running, last_tick_at=clock.last_tick_at)


class TickResult(BaseModel):
    sim_now: datetime
    real_seconds_advanced: float
    sim_seconds_advanced: float
    drivers_moved: int


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


@router.post("/tick", operation_id="simTick", response_model=TickResult)
async def sim_tick(_user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> TickResult:
    """Advance simulation clock + interpolate driver positions.

    Frontend calls this every few seconds while sim is running.
    Server advances sim_now by (real_elapsed * speed) and moves drivers
    along their routes proportional to sim_now between visita ETAs.
    """
    clock = await _get_clock(db, for_update=True)
    now_real = datetime.now(UTC)
    real_elapsed = (now_real - clock.last_tick_at).total_seconds()
    if not clock.running or real_elapsed <= 0:
        return TickResult(sim_now=clock.sim_now, real_seconds_advanced=0, sim_seconds_advanced=0, drivers_moved=0)

    sim_elapsed = real_elapsed * clock.speed
    clock.sim_now = clock.sim_now + timedelta(seconds=sim_elapsed)
    clock.last_tick_at = now_real

    # Advance drivers on active dias (EN_CURSO)
    drivers_moved = 0
    dias_result = await db.execute(select(DiaOperativo).where(DiaOperativo.estado == "EN_CURSO"))
    for dia in dias_result.scalars().all():
        rutas_result = await db.execute(select(Ruta).where(Ruta.dia_id == dia.dia_id))
        for ruta in rutas_result.scalars().all():
            # Get visitas for this ruta in order, only those with coordinates
            visitas_result = await db.execute(
                select(Visita).where(Visita.ruta_id == ruta.ruta_id).order_by(Visita.orden)
            )
            visitas = [
                v for v in visitas_result.scalars().all()
                if v.lat is not None and v.lon is not None
            ]
            if len(visitas) < 2:
                continue

            # Preload current driver position so we can preserve heading when
            # parked, and fall back to driver→next bearing on the very first
            # tick (CR-020 bug 3).
            pos_result = await db.execute(
                select(DriverPosition).where(DriverPosition.driver_id == ruta.driver_id)
            )
            pos = pos_result.scalar_one_or_none()
            prev_heading = pos.heading if (pos is not None and pos.heading is not None) else 0.0

            sim_now_t = clock.sim_now
            etas = [v.eta_estimada for v in visitas]
            has_etas = all(e is not None for e in etas)

            # `leg_idx` = index of the "from" visita on the active leg.
            # `next_visita` = next pending visita (the one the driver heads
            # toward). Used both for `driver_position.visita_id` and for
            # computing bearing.
            lat: float
            lon: float
            heading: float
            next_visita: Visita | None = None

            if has_etas:
                if sim_now_t < etas[0]:
                    # Before shift starts → driver parked at visita[0].
                    # `next_visita` stays as visita[0] (the upcoming pending
                    # visita per CR-020 contract for `driver_position.visita_id`).
                    # The cold-start heading seed (block below) handles the case
                    # where lat/lon == next_visita's coords by walking forward
                    # to the next distinct visita.
                    target = visitas[0]
                    lat, lon = target.lat, target.lon
                    heading = prev_heading
                    next_visita = visitas[0]
                elif sim_now_t >= etas[-1]:
                    # Route complete → parked at last visita.
                    target = visitas[-1]
                    lat, lon = target.lat, target.lon
                    heading = prev_heading  # CR-020: keep last heading, don't snap to 0
                    next_visita = None  # no pending visita anymore
                else:
                    leg_idx = 0
                    for i in range(len(etas) - 1):
                        if etas[i] <= sim_now_t < etas[i + 1]:
                            leg_idx = i
                            break
                    start_v = visitas[leg_idx]
                    end_v = visitas[leg_idx + 1]
                    leg_duration = (etas[leg_idx + 1] - etas[leg_idx]).total_seconds()
                    leg_t = (
                        (sim_now_t - etas[leg_idx]).total_seconds() / leg_duration
                        if leg_duration > 0
                        else 0.0
                    )
                    lat = _lerp(start_v.lat, end_v.lat, leg_t)
                    lon = _lerp(start_v.lon, end_v.lon, leg_t)
                    # Compass heading (0°=N, 90°=E) — atan2(Δlon, Δlat).
                    dy = end_v.lat - start_v.lat
                    dx = end_v.lon - start_v.lon
                    if dx == 0 and dy == 0:
                        heading = prev_heading
                    else:
                        heading = (math.degrees(math.atan2(dx, dy)) + 360) % 360
                    next_visita = end_v
            else:
                # Fallback: even distribution across 09:00-17:00 sim window.
                shift_start = datetime.combine(dia.fecha, datetime.min.time()).replace(hour=9, tzinfo=UTC)
                shift_end = shift_start.replace(hour=17)
                total_legs = len(visitas) - 1
                if sim_now_t < shift_start:
                    target = visitas[0]
                    lat, lon = target.lat, target.lon
                    heading = prev_heading
                    next_visita = visitas[0]
                elif sim_now_t >= shift_end:
                    target = visitas[-1]
                    lat, lon = target.lat, target.lon
                    heading = prev_heading
                    next_visita = None
                else:
                    progress_total = (sim_now_t - shift_start).total_seconds() / (shift_end - shift_start).total_seconds()
                    leg_position = progress_total * total_legs
                    leg_idx = min(int(leg_position), total_legs - 1)
                    leg_t = leg_position - leg_idx
                    start_v = visitas[leg_idx]
                    end_v = visitas[leg_idx + 1]
                    lat = _lerp(start_v.lat, end_v.lat, leg_t)
                    lon = _lerp(start_v.lon, end_v.lon, leg_t)
                    dy = end_v.lat - start_v.lat
                    dx = end_v.lon - start_v.lon
                    if dx == 0 and dy == 0:
                        heading = prev_heading
                    else:
                        heading = (math.degrees(math.atan2(dx, dy)) + 360) % 360
                    next_visita = end_v

            # Cold-start heading seed (CR-020 + CR-029):
            # If we end the tick on heading 0 with no previous heading (very
            # first tick before driver has ever moved, or pre-shift parking
            # at visita[0]), seed from current_pos → upcoming visita so the
            # map icon doesn't point straight north.
            #
            # CR-029 fix: when parked AT next_visita (lat/lon equal), walk
            # forward through `visitas` to find the first distinct destination
            # and use that for the bearing. Pre-shift cold-start was the
            # symptom — driver_position.visita_id stays as visita[0] (correct
            # per the contract), but the seed bearing now points to visita[1+].
            if heading == 0.0 and prev_heading == 0.0 and next_visita is not None:
                target_lat = next_visita.lat
                target_lon = next_visita.lon
                if target_lat == lat and target_lon == lon:
                    # Walk forward for the first distinct visita.
                    for upcoming in visitas:
                        if upcoming.lat != lat or upcoming.lon != lon:
                            target_lat = upcoming.lat
                            target_lon = upcoming.lon
                            break
                dy = target_lat - lat
                dx = target_lon - lon
                if dx != 0 or dy != 0:
                    heading = (math.degrees(math.atan2(dx, dy)) + 360) % 360

            # Upsert driver position. visita_id = next pending visita (CR-020 bug 3).
            visita_id_for_pos = next_visita.visita_id if next_visita is not None else None
            # Nominal modelled vehicle speed (km/h): a constant while en route,
            # 0 when parked at the final stop. The old value `30 + clock.speed*5`
            # leaked the sim ACCELERATION factor into the field (e.g. 330 at 60x),
            # which is not a real speed.
            nominal_speed = 0.0 if visita_id_for_pos is None else 30.0
            if pos:
                pos.lat = lat
                pos.lon = lon
                pos.heading = heading
                pos.speed = nominal_speed
                pos.visita_id = visita_id_for_pos
                pos.updated_at = now_real
            else:
                db.add(
                    DriverPosition(
                        driver_id=ruta.driver_id,
                        lat=lat,
                        lon=lon,
                        heading=heading,
                        speed=nominal_speed,
                        visita_id=visita_id_for_pos,
                        updated_at=now_real,
                    )
                )
            drivers_moved += 1

    await db.commit()
    return TickResult(sim_now=clock.sim_now, real_seconds_advanced=real_elapsed, sim_seconds_advanced=sim_elapsed, drivers_moved=drivers_moved)

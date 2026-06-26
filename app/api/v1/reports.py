"""Operational reporting endpoints.

GET /api/v1/reports/dia/{dia_id} — single-day report: delivery outcomes,
region breakdown, driver behaviour, non-delivery reasons, punctuality, and a
comparison against the empresa's previous día (by fecha).

Aggregation is pushed into SQL (`GROUP BY`) rather than hydrating every visita
as an ORM object — at 20k visitas the ORM path took ~27s; the grouped queries
return a handful of rows each. The only per-row fetch is the punctuality pair
(eta, completada) as lightweight tuples (no ORM), filtered to measured rows.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user
from app.core.security.scope import can_access_empresa
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db
from app.schemas.report import (
    Comparison,
    DiaReport,
    DriverRow,
    MotivoRow,
    OnTime,
    OutcomeCounts,
    RangeReport,
    RegionRow,
    TrendPoint,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def _pct(num: int, den: int) -> float | None:
    return round(100 * num / den, 1) if den else None


def _success_pct(c: dict) -> float | None:
    term = c.get("entregado", 0) + c.get("no_entregado", 0) + c.get("cancelado", 0)
    return _pct(c.get("entregado", 0), term)


def _outcome(c: dict) -> OutcomeCounts:
    return OutcomeCounts(
        visitas=sum(c.values()),
        entregado=c.get("entregado", 0),
        no_entregado=c.get("no_entregado", 0),
        cancelado=c.get("cancelado", 0),
        pendiente=c.get("pendiente", 0),
        success_pct=_success_pct(c),
    )


async def _estado_counts(db: AsyncSession, dia_ids: list[int], *, vip_only: bool = False) -> dict:
    """{estado: count} across one or more días via SQL GROUP BY."""
    if not dia_ids:
        return {}
    stmt = select(Visita.estado, func.count()).where(Visita.dia_id.in_(dia_ids)).group_by(Visita.estado)
    if vip_only:
        stmt = stmt.where(Visita.es_vip == 1)
    return {estado: n for estado, n in (await db.execute(stmt)).all()}


def _delay_seconds(db: AsyncSession):
    """Dialect-aware (completada - eta) in seconds, computed IN the database so
    we never transfer per-row timestamps. MSSQL uses DATEDIFF; SQLite (tests)
    uses julianday()."""
    eta, comp = Visita.eta_estimada, Visita.completada_at
    name = db.get_bind().dialect.name
    if name == "mssql":
        # DATEDIFF_BIG (BIGINT), not DATEDIFF (INT): SUM() over many rows with
        # large eta→completada gaps overflows a 32-bit INT accumulator on SQL
        # Server (caused 500s on días with lots of delivered visitas).
        return func.datediff_big(text("second"), eta, comp)
    return (func.julianday(comp) - func.julianday(eta)) * 86400.0


def _on_time_row(medidas, a_tiempo, sum_delay_sec, grace: int) -> OnTime:
    medidas = int(medidas or 0)
    a_tiempo = int(a_tiempo or 0)
    avg = round((float(sum_delay_sec) / medidas) / 60.0, 1) if medidas else None
    return OnTime(
        medidas=medidas,
        a_tiempo=a_tiempo,
        atrasadas=medidas - a_tiempo,
        on_time_pct=_pct(a_tiempo, medidas),
        avg_delay_min=avg,
        grace_min=grace,
    )


async def _on_time_overall(db: AsyncSession, dia_ids: list[int], grace: int) -> OnTime:
    if not dia_ids:
        return _on_time_row(0, 0, 0, grace)
    delay = _delay_seconds(db)
    row = (await db.execute(
        select(
            func.count(),
            func.sum(case((delay <= grace * 60, 1), else_=0)),
            func.coalesce(func.sum(delay), 0),
        ).where(
            Visita.dia_id.in_(dia_ids),
            Visita.eta_estimada.isnot(None),
            Visita.completada_at.isnot(None),
        )
    )).one()
    return _on_time_row(row[0], row[1], row[2], grace)


async def _on_time_by_driver(db: AsyncSession, dia_ids: list[int], grace: int) -> dict:
    if not dia_ids:
        return {}
    delay = _delay_seconds(db)
    rows = (await db.execute(
        select(
            Ruta.driver_id,
            func.count(),
            func.sum(case((delay <= grace * 60, 1), else_=0)),
            func.coalesce(func.sum(delay), 0),
        )
        .select_from(Visita)
        .join(Ruta, Visita.ruta_id == Ruta.ruta_id, isouter=True)
        .where(
            Visita.dia_id.in_(dia_ids),
            Visita.eta_estimada.isnot(None),
            Visita.completada_at.isnot(None),
        )
        .group_by(Ruta.driver_id)
    )).all()
    return {did: _on_time_row(n, a, s, grace) for did, n, a, s in rows}


def _delta(a: float | None, b: float | None) -> float | None:
    return round(a - b, 1) if (a is not None and b is not None) else None


async def _day_summary(db: AsyncSession, dia_id: int, grace: int) -> tuple[OutcomeCounts, OnTime]:
    """Totals + punctuality for a día (used for the comparison baseline)."""
    counts = await _estado_counts(db, [dia_id])
    on_time = await _on_time_overall(db, [dia_id], grace)
    return _outcome(counts), on_time


async def _aggregate(
    db: AsyncSession, dia_ids: list[int], grace: int,
) -> tuple[OutcomeCounts, OutcomeCounts, OnTime, list[RegionRow], list[DriverRow], list[MotivoRow]]:
    """Totals, VIP, punctuality and region/driver/motivo breakdowns across one or
    more días. Shared by the single-día and date-range report endpoints; every
    aggregation is pushed into SQL (GROUP BY) so it scales with the result set,
    not the visita count."""
    totals = _outcome(await _estado_counts(db, dia_ids))
    vip = _outcome(await _estado_counts(db, dia_ids, vip_only=True))
    on_time = await _on_time_overall(db, dia_ids, grace)
    driver_ot = await _on_time_by_driver(db, dia_ids, grace)

    if not dia_ids:
        return totals, vip, on_time, [], [], []

    # ---- by region (grouped) ----
    region_rows = (await db.execute(
        select(Visita.region, Visita.estado, func.count())
        .where(Visita.dia_id.in_(dia_ids))
        .group_by(Visita.region, Visita.estado)
    )).all()
    region_acc: dict = defaultdict(lambda: defaultdict(int))
    for region, estado, n in region_rows:
        region_acc[region][estado] = n
    by_region = sorted(
        (
            RegionRow(
                region=reg, visitas=sum(c.values()),
                entregado=c.get("entregado", 0), no_entregado=c.get("no_entregado", 0),
                success_pct=_success_pct(c),
            )
            for reg, c in region_acc.items()
        ),
        key=lambda r: r.visitas, reverse=True,
    )

    # ---- by driver (grouped counts + names + punctuality) ----
    driver_rows = (await db.execute(
        select(Ruta.driver_id, Visita.estado, func.count())
        .select_from(Visita)
        .join(Ruta, Visita.ruta_id == Ruta.ruta_id, isouter=True)
        .where(Visita.dia_id.in_(dia_ids))
        .group_by(Ruta.driver_id, Visita.estado)
    )).all()
    driver_acc: dict = defaultdict(lambda: defaultdict(int))
    for did, estado, n in driver_rows:
        driver_acc[did][estado] = n
    driver_ids = [d for d in driver_acc if d is not None]
    names = dict((await db.execute(
        select(Driver.driver_id, Driver.nombre).where(Driver.driver_id.in_(driver_ids))
    )).all()) if driver_ids else {}
    by_driver = sorted(
        (
            DriverRow(
                driver_id=did, nombre=names.get(did),
                visitas=sum(c.values()),
                entregado=c.get("entregado", 0), no_entregado=c.get("no_entregado", 0),
                cancelado=c.get("cancelado", 0),
                success_pct=_success_pct(c),
                on_time_pct=(driver_ot.get(did).on_time_pct if driver_ot.get(did) else None),
                avg_delay_min=(driver_ot.get(did).avg_delay_min if driver_ot.get(did) else None),
            )
            for did, c in driver_acc.items()
        ),
        key=lambda r: r.visitas, reverse=True,
    )

    # ---- by motivo (grouped) ----
    motivo_rows = (await db.execute(
        select(Visita.motivo, func.count())
        .where(
            Visita.dia_id.in_(dia_ids),
            Visita.estado.in_(("no_entregado", "cancelado")),
            Visita.motivo.isnot(None),
        )
        .group_by(Visita.motivo)
    )).all()
    by_motivo = sorted(
        (MotivoRow(motivo=m, count=n) for m, n in motivo_rows),
        key=lambda r: r.count, reverse=True,
    )
    return totals, vip, on_time, by_region, by_driver, by_motivo


@router.get("/dia/{dia_id}", operation_id="getDiaReport", response_model=DiaReport)
async def get_dia_report(
    dia_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> DiaReport:
    dia = (await db.execute(
        select(DiaOperativo).where(DiaOperativo.dia_id == dia_id)
    )).scalar_one_or_none()
    if dia is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Día no encontrado")
    if not can_access_empresa(user, dia.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sin acceso a esta empresa")

    empresa_nombre = await db.scalar(
        select(Empresa.nombre).where(Empresa.empresa_id == dia.empresa_id)
    )
    grace = settings.alerts_grace_min

    totals, vip, on_time, by_region, by_driver, by_motivo = await _aggregate(db, [dia_id], grace)

    # ---- comparison vs the empresa's previous día ----
    prev = (await db.execute(
        select(DiaOperativo)
        .where(DiaOperativo.empresa_id == dia.empresa_id, DiaOperativo.fecha < dia.fecha)
        .order_by(DiaOperativo.fecha.desc())
        .limit(1)
    )).scalar_one_or_none()
    if prev is not None:
        prev_totals, prev_on_time = await _day_summary(db, prev.dia_id, grace)
        comparison = Comparison(
            prev_dia_id=prev.dia_id, prev_fecha=prev.fecha,
            visitas_delta=totals.visitas - prev_totals.visitas,
            success_pct_delta=_delta(totals.success_pct, prev_totals.success_pct),
            on_time_pct_delta=_delta(on_time.on_time_pct, prev_on_time.on_time_pct),
        )
    else:
        comparison = Comparison(
            prev_dia_id=None, prev_fecha=None,
            visitas_delta=None, success_pct_delta=None, on_time_pct_delta=None,
        )

    return DiaReport(
        dia_id=dia.dia_id, fecha=dia.fecha, empresa_id=dia.empresa_id,
        empresa_nombre=empresa_nombre, estado=dia.estado,
        totals=totals, vip=vip, on_time=on_time,
        by_region=by_region, by_driver=by_driver, by_motivo=by_motivo,
        comparison=comparison,
    )


async def _trend(db: AsyncSession, dias: list[tuple[int, date]], grace: int) -> list[TrendPoint]:
    """Per-día trend across a range, in two grouped queries (not N)."""
    dia_ids = [d for d, _ in dias]
    if not dia_ids:
        return []
    # totals + entregado per día
    est_rows = (await db.execute(
        select(Visita.dia_id, Visita.estado, func.count())
        .where(Visita.dia_id.in_(dia_ids))
        .group_by(Visita.dia_id, Visita.estado)
    )).all()
    est_acc: dict = defaultdict(lambda: defaultdict(int))
    for did, estado, n in est_rows:
        est_acc[did][estado] = n
    # punctuality per día
    delay = _delay_seconds(db)
    ot_rows = (await db.execute(
        select(
            Visita.dia_id,
            func.count(),
            func.sum(case((delay <= grace * 60, 1), else_=0)),
        )
        .where(
            Visita.dia_id.in_(dia_ids),
            Visita.eta_estimada.isnot(None),
            Visita.completada_at.isnot(None),
        )
        .group_by(Visita.dia_id)
    )).all()
    ot_acc = {did: (int(n or 0), int(a or 0)) for did, n, a in ot_rows}

    points = []
    for did, fecha in dias:
        c = est_acc.get(did, {})
        medidas, a_tiempo = ot_acc.get(did, (0, 0))
        points.append(TrendPoint(
            fecha=fecha, dia_id=did,
            visitas=sum(c.values()),
            entregado=c.get("entregado", 0),
            success_pct=_success_pct(c),
            on_time_pct=_pct(a_tiempo, medidas),
        ))
    return points


@router.get("/rango", operation_id="getRangeReport", response_model=RangeReport)
async def get_range_report(
    empresa_id: int = Query(..., description="Empresa a reportar"),
    desde: date = Query(..., description="Fecha inicial (inclusive)"),
    hasta: date = Query(..., description="Fecha final (inclusive)"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> RangeReport:
    """Aggregate report over every día of an empresa within [desde, hasta]."""
    if desde > hasta:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "'desde' debe ser <= 'hasta'")
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sin acceso a esta empresa")

    empresa_nombre = await db.scalar(
        select(Empresa.nombre).where(Empresa.empresa_id == empresa_id)
    )
    grace = settings.alerts_grace_min

    dias = (await db.execute(
        select(DiaOperativo.dia_id, DiaOperativo.fecha)
        .where(
            DiaOperativo.empresa_id == empresa_id,
            DiaOperativo.fecha >= desde,
            DiaOperativo.fecha <= hasta,
        )
        .order_by(DiaOperativo.fecha)
    )).all()
    dia_ids = [d for d, _ in dias]

    totals, vip, on_time, by_region, by_driver, by_motivo = await _aggregate(db, dia_ids, grace)
    trend = await _trend(db, dias, grace)

    return RangeReport(
        empresa_id=empresa_id, empresa_nombre=empresa_nombre,
        desde=desde, hasta=hasta, dias=len(dia_ids),
        totals=totals, vip=vip, on_time=on_time,
        by_region=by_region, by_driver=by_driver, by_motivo=by_motivo,
        trend=trend,
    )

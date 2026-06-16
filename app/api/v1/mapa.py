"""Mapa operativo — aggregated/cross-empresa views for the live map (CR-029).

Three GET endpoints that serve the deck.gl map and KPI side panels:

  - GET /api/v1/mapa/visitas    → flat visita list with display-ready fields.
  - GET /api/v1/mapa/heatmap    → comuna-grouped buckets for HexagonLayer.
  - GET /api/v1/mapa/stats      → KPIs per empresa + worst/best rankings.

Scope:
  * `falabella_admin` / `falabella_ops` see ALL empresas. They may narrow with
    `empresa_ids=2,5` (CSV).
  * `transport_manager` sees only their assigned empresas. If they pass
    `empresa_ids` containing IDs outside their scope, those IDs are silently
    dropped — same pattern as `apply_scope` in operacion.list_dias (the AND-ed
    where wins). We chose silent filtering over 403 to avoid leaking which
    empresa IDs exist via probing.

Performance:
  * All three endpoints rely on SQL-side aggregation; the heaviest path is
    `/heatmap` with a single GROUP BY on `visitas`. No N+1 fan-out: empresa
    names are resolved via a single `IN (...)` and stitched in Python.
  * `atraso_min` is computed against `sim_clock.sim_now` (not wall clock) so
    the map matches the simulator state during demos.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.geocoding import centroide_comuna
from app.core.security import current_user
from app.core.security.scope import apply_scope
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/mapa", tags=["mapa"])


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------


class MapaVisitaItem(BaseModel):
    visita_id: int
    ruta_id: int | None
    ruta_folio: str | None
    dia_id: int
    empresa_id: int
    empresa_nombre: str
    cliente_nombre: str
    direccion: str
    comuna: str | None
    lat: float | None
    lon: float | None
    estado: str
    es_vip: bool
    eta_estimada: datetime | None
    atraso_min: int | None = Field(
        default=None,
        description=(
            "Minutos de atraso de una visita pendiente: "
            "max(0, sim_now - eta_estimada). null si no aplica."
        ),
    )
    folio_cliente: str | None


class MapaHeatmapBucket(BaseModel):
    comuna: str
    lat: float
    lon: float
    total: int
    entregadas: int
    no_entregadas: int
    pendientes: int
    atrasadas: int = Field(
        description="Pendientes con eta_estimada < sim_now - 15min."
    )
    canceladas: int


class MapaHeatmapResponse(BaseModel):
    fecha: date
    buckets: list[MapaHeatmapBucket]


class EmpresaStat(BaseModel):
    empresa_id: int
    empresa_nombre: str
    rutas: int
    visitas: int
    entregadas: int
    no_entregadas: int
    atrasadas: int
    pendientes: int
    avance_pct: float = Field(
        description="(entregadas + no_entregadas) / visitas * 100; 0 si sin visitas."
    )


class TopComuna(BaseModel):
    comuna: str
    no_entregadas: int


class TopRuta(BaseModel):
    ruta_id: int
    ruta_folio: str
    empresa_nombre: str
    avance_pct: float
    total_visitas: int


class MapaStatsResponse(BaseModel):
    fecha: date
    total_visitas: int
    total_entregadas: int
    total_no_entregadas: int
    total_atrasadas: int
    avance_pct: float
    por_empresa: list[EmpresaStat]
    top_comunas_fails: list[TopComuna]
    top_rutas_best: list[TopRuta]
    top_rutas_worst: list[TopRuta]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

_DEFAULT_VALID_ESTADOS = {
    "pendiente",
    "en_camino",
    "entregado",
    "no_entregado",
    "cancelado",
}

# A visita is considered "atrasada" if it is still pending (or en_camino) AND
# its eta_estimada is older than sim_now minus this grace window. Mirrors the
# threshold used by the eta_breach cron.
_ATRASO_GRACE_MIN = 15


def _parse_empresa_ids(raw: str | None) -> list[int] | None:
    """CSV `2,5,12` → `[2, 5, 12]`. Empty / None → None (no filter)."""
    if not raw:
        return None
    out: list[int] = []
    for raw_tok in raw.split(","):
        tok = raw_tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"empresa_ids inválido: {tok!r} no es entero",
            ) from e
    return out or None


def _parse_estados(raw: str | None) -> set[str] | None:
    """CSV estados; None / empty → no filter. Invalid token → 400."""
    if not raw:
        return None
    out: set[str] = set()
    for raw_tok in raw.split(","):
        tok = raw_tok.strip().lower()
        if not tok:
            continue
        if tok not in _DEFAULT_VALID_ESTADOS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"estado inválido: {tok!r}",
            )
        out.add(tok)
    return out or None


async def _get_sim_now(db: AsyncSession) -> datetime:
    """Best-effort sim clock read. Falls back to wall-clock if no row exists.

    This is read-only — we never `INSERT` from a map endpoint. If the sim_clock
    row is missing (fresh DB), wall clock is fine: atraso_min only matters once
    operators start the simulator anyway.
    """
    clock = (
        await db.execute(select(SimClock).where(SimClock.id == 1))
    ).scalar_one_or_none()
    if clock is None:
        return datetime.now(UTC)
    return clock.sim_now


async def _empresa_lookup(
    db: AsyncSession, empresa_ids: list[int]
) -> dict[int, str]:
    """Bulk fetch empresa_id → nombre for the given ids (preserves order N/A)."""
    if not empresa_ids:
        return {}
    rows = (
        await db.execute(
            select(Empresa.empresa_id, Empresa.nombre).where(
                Empresa.empresa_id.in_(empresa_ids)
            )
        )
    ).all()
    return {row.empresa_id: row.nombre for row in rows}


def _compute_atraso_min(
    eta: datetime | None, sim_now: datetime, estado: str
) -> int | None:
    """Atraso real (minutos) si la visita sigue pendiente y eta < sim_now.

    Returns None for visitas in a terminal estado or without eta. Never
    negative: a pending visita whose ETA is still in the future has atraso = 0.
    """
    if eta is None:
        return None
    if estado not in ("pendiente", "en_camino"):
        return None
    delta = (sim_now - eta).total_seconds() / 60.0
    return max(0, int(delta))


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------


@router.get(
    "/visitas",
    operation_id="listMapaVisitas",
    response_model=list[MapaVisitaItem],
)
async def list_mapa_visitas(
    fecha: date | None = Query(default=None, description="Default: today (UTC)."),
    empresa_ids: str | None = Query(
        default=None,
        description="CSV de empresa_id. Default: todas las accesibles al user.",
    ),
    estados: str | None = Query(
        default=None,
        description=(
            "CSV de estados (pendiente,en_camino,entregado,no_entregado,cancelado)."
            " Default: todos."
        ),
    ),
    solo_vip: bool = Query(default=False),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MapaVisitaItem]:
    """Flat visita list ready to plot on the map.

    Joins `visitas` → `dias_operativos` (for `empresa_id`, scope) → `rutas`
    (for folio) → `empresas` (for `nombre`).

    Scope behaviour:
      * `transport_manager` with `empresa_ids` that include outside-scope IDs
        gets only their own subset (silent filter, no 403).
    """
    target_fecha = fecha or datetime.now(UTC).date()
    requested_empresas = _parse_empresa_ids(empresa_ids)
    estado_set = _parse_estados(estados)

    # Pull dias for the target date, scoped to the user.
    dias_stmt = select(DiaOperativo).where(DiaOperativo.fecha == target_fecha)
    dias_stmt = apply_scope(dias_stmt, user, DiaOperativo.empresa_id)
    if requested_empresas is not None:
        dias_stmt = dias_stmt.where(
            DiaOperativo.empresa_id.in_(requested_empresas)
        )
    dias = (await db.execute(dias_stmt)).scalars().all()
    if not dias:
        return []

    dia_ids = [d.dia_id for d in dias]

    # Pull all visitas for those dias, with optional filters.
    visitas_stmt = (
        select(Visita)
        .where(Visita.dia_id.in_(dia_ids))
        .order_by(Visita.empresa_id, Visita.ruta_id, Visita.orden)
    )
    if estado_set is not None:
        visitas_stmt = visitas_stmt.where(Visita.estado.in_(estado_set))
    if solo_vip:
        visitas_stmt = visitas_stmt.where(Visita.es_vip == 1)
    visitas = (await db.execute(visitas_stmt)).scalars().all()
    if not visitas:
        return []

    # Bulk-load empresas + rutas to avoid N+1.
    empresa_ids_seen = sorted({v.empresa_id for v in visitas})
    empresa_names = await _empresa_lookup(db, empresa_ids_seen)

    ruta_ids = sorted({v.ruta_id for v in visitas if v.ruta_id is not None})
    ruta_folios: dict[int, str | None] = {}
    if ruta_ids:
        rows = (
            await db.execute(
                select(Ruta.ruta_id, Ruta.folio).where(Ruta.ruta_id.in_(ruta_ids))
            )
        ).all()
        ruta_folios = {row.ruta_id: row.folio for row in rows}

    sim_now = await _get_sim_now(db)

    out: list[MapaVisitaItem] = []
    for v in visitas:
        out.append(
            MapaVisitaItem(
                visita_id=v.visita_id,
                ruta_id=v.ruta_id,
                ruta_folio=ruta_folios.get(v.ruta_id) if v.ruta_id else None,
                dia_id=v.dia_id,
                empresa_id=v.empresa_id,
                empresa_nombre=empresa_names.get(v.empresa_id, ""),
                cliente_nombre=v.cliente_nombre,
                direccion=v.direccion,
                comuna=v.comuna,
                lat=v.lat,
                lon=v.lon,
                estado=v.estado,
                es_vip=bool(v.es_vip),
                eta_estimada=v.eta_estimada,
                atraso_min=_compute_atraso_min(v.eta_estimada, sim_now, v.estado),
                folio_cliente=v.folio_cliente,
            )
        )
    return out


@router.get(
    "/heatmap",
    operation_id="getMapaHeatmap",
    response_model=MapaHeatmapResponse,
)
async def get_mapa_heatmap(
    fecha: date | None = Query(default=None),
    empresa_ids: str | None = Query(default=None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MapaHeatmapResponse:
    """Aggregate by comuna for HexagonLayer / heatmap rendering.

    One row per comuna with: total / entregadas / no_entregadas / pendientes /
    atrasadas / canceladas. Latitude/longitude are the comuna centroid from
    `core.geocoding._COMUNA_CENTROIDS`. Comunas not present in the centroid
    table are SKIPPED (the heatmap is best-effort visual; missing centroids
    would render at lat=0,lon=0).
    """
    target_fecha = fecha or datetime.now(UTC).date()
    requested_empresas = _parse_empresa_ids(empresa_ids)

    dias_stmt = select(DiaOperativo.dia_id).where(
        DiaOperativo.fecha == target_fecha
    )
    dias_stmt = apply_scope(dias_stmt, user, DiaOperativo.empresa_id)
    if requested_empresas is not None:
        dias_stmt = dias_stmt.where(
            DiaOperativo.empresa_id.in_(requested_empresas)
        )
    dia_ids = [r[0] for r in (await db.execute(dias_stmt)).all()]
    if not dia_ids:
        return MapaHeatmapResponse(fecha=target_fecha, buckets=[])

    sim_now = await _get_sim_now(db)
    atraso_cutoff = sim_now - timedelta(minutes=_ATRASO_GRACE_MIN)

    # Single GROUP BY query. We do the comuna centroid join in Python because
    # the table is hardcoded — pushing it to SQL would require a CTE per call.
    estado_sum = lambda val: func.sum(  # noqa: E731
        case((Visita.estado == val, 1), else_=0)
    )
    atrasadas_sum = func.sum(
        case(
            (
                (Visita.estado.in_(("pendiente", "en_camino")))
                & (Visita.eta_estimada.is_not(None))
                & (Visita.eta_estimada < atraso_cutoff),
                1,
            ),
            else_=0,
        )
    )

    stmt = (
        select(
            Visita.comuna.label("comuna"),
            func.count().label("total"),
            estado_sum("entregado").label("entregadas"),
            estado_sum("no_entregado").label("no_entregadas"),
            estado_sum("pendiente").label("pendientes_estado"),
            estado_sum("en_camino").label("en_camino"),
            estado_sum("cancelado").label("canceladas"),
            atrasadas_sum.label("atrasadas"),
        )
        .where(Visita.dia_id.in_(dia_ids))
        .where(Visita.comuna.is_not(None))
        .group_by(Visita.comuna)
    )
    rows = (await db.execute(stmt)).all()

    buckets: list[MapaHeatmapBucket] = []
    for row in rows:
        centroide = centroide_comuna(row.comuna)
        if centroide is None:
            # No centroid → don't fabricate a position. Skip and let the
            # frontend show a "unknown comuna" badge if needed.
            continue
        lat, lon = centroide
        # "pendientes" in the response merges {pendiente, en_camino}: from the
        # operator's point of view both are "still out there".
        pendientes_total = int(
            (row.pendientes_estado or 0) + (row.en_camino or 0)
        )
        buckets.append(
            MapaHeatmapBucket(
                comuna=row.comuna,
                lat=lat,
                lon=lon,
                total=int(row.total or 0),
                entregadas=int(row.entregadas or 0),
                no_entregadas=int(row.no_entregadas or 0),
                pendientes=pendientes_total,
                atrasadas=int(row.atrasadas or 0),
                canceladas=int(row.canceladas or 0),
            )
        )
    # Stable ordering for client-side consistency.
    buckets.sort(key=lambda b: b.total, reverse=True)
    return MapaHeatmapResponse(fecha=target_fecha, buckets=buckets)


@router.get(
    "/stats",
    operation_id="getMapaStats",
    response_model=MapaStatsResponse,
)
async def get_mapa_stats(  # noqa: PLR0915
    fecha: date | None = Query(default=None),
    empresa_ids: str | None = Query(default=None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MapaStatsResponse:
    """KPIs agregados para los paneles del mapa.

    Returns global totals + per-empresa breakdown + top fail comunas + best/worst
    ruta rankings (min 5 visitas to enter the ranking — small rutas have too
    much variance to compare fairly).
    """
    target_fecha = fecha or datetime.now(UTC).date()
    requested_empresas = _parse_empresa_ids(empresa_ids)

    dias_stmt = select(DiaOperativo).where(DiaOperativo.fecha == target_fecha)
    dias_stmt = apply_scope(dias_stmt, user, DiaOperativo.empresa_id)
    if requested_empresas is not None:
        dias_stmt = dias_stmt.where(
            DiaOperativo.empresa_id.in_(requested_empresas)
        )
    dias = (await db.execute(dias_stmt)).scalars().all()
    if not dias:
        return MapaStatsResponse(
            fecha=target_fecha,
            total_visitas=0,
            total_entregadas=0,
            total_no_entregadas=0,
            total_atrasadas=0,
            avance_pct=0.0,
            por_empresa=[],
            top_comunas_fails=[],
            top_rutas_best=[],
            top_rutas_worst=[],
        )

    dia_ids = [d.dia_id for d in dias]
    empresa_ids_in_scope = sorted({d.empresa_id for d in dias})
    empresa_names = await _empresa_lookup(db, empresa_ids_in_scope)

    sim_now = await _get_sim_now(db)
    atraso_cutoff = sim_now - timedelta(minutes=_ATRASO_GRACE_MIN)

    # Reusable CASE helpers. `is_cancelado` is not summed in the rollup but
    # we keep the symbol available for future extension (e.g. per-empresa
    # canceladas count). Currently unused, ignored by ruff via leading _.
    is_entregado = case((Visita.estado == "entregado", 1), else_=0)
    is_no_entregado = case((Visita.estado == "no_entregado", 1), else_=0)
    is_pending = case(
        (Visita.estado.in_(("pendiente", "en_camino")), 1), else_=0
    )
    is_atrasada = case(
        (
            (Visita.estado.in_(("pendiente", "en_camino")))
            & (Visita.eta_estimada.is_not(None))
            & (Visita.eta_estimada < atraso_cutoff),
            1,
        ),
        else_=0,
    )

    # 1) per-empresa rollup.
    rutas_count_stmt = (
        select(Ruta.dia_id, func.count(Ruta.ruta_id).label("n"))
        .where(Ruta.dia_id.in_(dia_ids))
        .group_by(Ruta.dia_id)
    )
    rutas_per_dia = {r.dia_id: int(r.n) for r in (await db.execute(rutas_count_stmt)).all()}

    visitas_rollup_stmt = (
        select(
            Visita.empresa_id.label("empresa_id"),
            func.count().label("visitas"),
            func.sum(is_entregado).label("entregadas"),
            func.sum(is_no_entregado).label("no_entregadas"),
            func.sum(is_pending).label("pendientes"),
            func.sum(is_atrasada).label("atrasadas"),
        )
        .where(Visita.dia_id.in_(dia_ids))
        .group_by(Visita.empresa_id)
    )
    visitas_rollup = {
        row.empresa_id: row for row in (await db.execute(visitas_rollup_stmt)).all()
    }

    # Map empresa_id → set(dia_id) so we can sum rutas across dias of same empresa.
    rutas_per_empresa: dict[int, int] = {}
    for d in dias:
        rutas_per_empresa[d.empresa_id] = (
            rutas_per_empresa.get(d.empresa_id, 0)
            + rutas_per_dia.get(d.dia_id, 0)
        )

    por_empresa: list[EmpresaStat] = []
    total_visitas = 0
    total_entregadas = 0
    total_no_entregadas = 0
    total_atrasadas = 0
    for eid in empresa_ids_in_scope:
        roll = visitas_rollup.get(eid)
        visitas = int(roll.visitas) if roll else 0
        entregadas = int(roll.entregadas or 0) if roll else 0
        no_entregadas = int(roll.no_entregadas or 0) if roll else 0
        pendientes = int(roll.pendientes or 0) if roll else 0
        atrasadas = int(roll.atrasadas or 0) if roll else 0
        avance_pct = (
            ((entregadas + no_entregadas) / visitas * 100) if visitas else 0.0
        )
        por_empresa.append(
            EmpresaStat(
                empresa_id=eid,
                empresa_nombre=empresa_names.get(eid, ""),
                rutas=rutas_per_empresa.get(eid, 0),
                visitas=visitas,
                entregadas=entregadas,
                no_entregadas=no_entregadas,
                atrasadas=atrasadas,
                pendientes=pendientes,
                avance_pct=round(avance_pct, 2),
            )
        )
        total_visitas += visitas
        total_entregadas += entregadas
        total_no_entregadas += no_entregadas
        total_atrasadas += atrasadas

    global_avance = (
        ((total_entregadas + total_no_entregadas) / total_visitas * 100)
        if total_visitas
        else 0.0
    )

    # 2) top comunas with most no_entregadas (top 5).
    top_comunas_stmt = (
        select(
            Visita.comuna.label("comuna"),
            func.sum(is_no_entregado).label("fails"),
        )
        .where(Visita.dia_id.in_(dia_ids))
        .where(Visita.comuna.is_not(None))
        .group_by(Visita.comuna)
        .order_by(func.sum(is_no_entregado).desc())
        .limit(5)
    )
    top_comunas_fails = [
        TopComuna(comuna=row.comuna, no_entregadas=int(row.fails or 0))
        for row in (await db.execute(top_comunas_stmt)).all()
        if (row.fails or 0) > 0
    ]

    # 3) rutas with avance_pct + total_visitas (need both for filter + rank).
    rutas_stmt = (
        select(
            Ruta.ruta_id.label("ruta_id"),
            Ruta.dia_id.label("dia_id"),
            Ruta.folio.label("folio"),
            func.count(Visita.visita_id).label("total"),
            func.sum(is_entregado + is_no_entregado).label("done"),
        )
        .join(Visita, Visita.ruta_id == Ruta.ruta_id)
        .where(Ruta.dia_id.in_(dia_ids))
        .group_by(Ruta.ruta_id, Ruta.dia_id, Ruta.folio)
        .having(func.count(Visita.visita_id) >= 5)
    )
    rutas_rows = (await db.execute(rutas_stmt)).all()
    dia_to_empresa = {d.dia_id: d.empresa_id for d in dias}
    candidates: list[TopRuta] = []
    for row in rutas_rows:
        total = int(row.total or 0)
        if total == 0:
            continue
        done = int(row.done or 0)
        pct = round(done / total * 100, 2)
        eid = dia_to_empresa.get(row.dia_id, 0)
        candidates.append(
            TopRuta(
                ruta_id=row.ruta_id,
                ruta_folio=row.folio or f"ruta-{row.ruta_id}",
                empresa_nombre=empresa_names.get(eid, ""),
                avance_pct=pct,
                total_visitas=total,
            )
        )
    candidates_best = sorted(candidates, key=lambda r: r.avance_pct, reverse=True)[:3]
    candidates_worst = sorted(candidates, key=lambda r: r.avance_pct)[:3]

    return MapaStatsResponse(
        fecha=target_fecha,
        total_visitas=total_visitas,
        total_entregadas=total_entregadas,
        total_no_entregadas=total_no_entregadas,
        total_atrasadas=total_atrasadas,
        avance_pct=round(global_avance, 2),
        por_empresa=por_empresa,
        top_comunas_fails=top_comunas_fails,
        top_rutas_best=candidates_best,
        top_rutas_worst=candidates_worst,
    )

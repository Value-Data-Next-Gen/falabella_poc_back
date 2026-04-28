"""Dashboard de seguimiento sobre fpoc.simpli_visits + fpoc.geo_suborders.

Lee la data real cargada a Azure SQL. Scope multi-tenant por
Empresa_falsa = user.empresa_id cuando el rol es transport_manager.

Todos los endpoints aceptan ?planned_date=YYYY-MM-DD.
Si no se provee, se usa MAX(planned_date) en la tabla (última fecha disponible).

Endpoints (prefijo /api/seguimiento):
  GET /available-dates
  GET /kpis                 (?planned_date)
  GET /sla-distribution     (?planned_date)
  GET /motivos
  GET /by-empresa           (?planned_date)
  GET /by-localidad
  GET /rutas-anomalas       (?planned_date)
  GET /visits               (paginado con filtros + planned_date)
"""
from __future__ import annotations

from datetime import date as date_cls
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from auth import CurrentUser, current_user
from db import get_conn

router = APIRouter(prefix="/api/seguimiento", tags=["seguimiento"])


# ---------- Scope + date helpers ----------
def _scope(user: CurrentUser, alias: str = "s") -> tuple[str, list]:
    """Devuelve (clausula_sql, params) para aplicar scope por empresa."""
    if user.is_falabella:
        return "", []
    return f" AND {alias}.Empresa_falsa = ?", [user.empresa_id]


def _resolve_date(pd: Optional[str]) -> date_cls:
    if pd:
        return date_cls.fromisoformat(pd)
    # Default: la fecha máxima disponible en la tabla
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT MAX(planned_date) FROM fpoc.simpli_visits")
        r = cur.fetchone()
        if r and r[0]:
            v = r[0]
            # SQLite agregados pierden el type binding → puede venir como str.
            if isinstance(v, str):
                return date_cls.fromisoformat(v.split(" ")[0])
            return v
    return date_cls.today()


def _date_scope(user: CurrentUser, planned_date: Optional[str], alias: str = "s") -> tuple[str, list, str]:
    """Combina filtro empresa + fecha. Devuelve (where_fragment, params, resolved_date_iso)."""
    d = _resolve_date(planned_date)
    where = f" AND {alias}.planned_date = ?"
    params: list = [d]
    scope_w, scope_p = _scope(user, alias)
    where += scope_w
    params.extend(scope_p)
    return where, params, d.isoformat()


# ---------- Schemas ----------
class KPIs(BaseModel):
    planned_date: str
    total: int
    completed: int
    failed: int
    completion_pct: float
    ruta_anomala: int
    ruta_anomala_pct: float
    sla_hour_avg: float
    sla_hour_p50: float
    sla_hour_p90: float
    on_time: int  # abs(sla_hour) <= 1
    early: int    # sla_hour < -1 (llegó temprano)
    late: int     # sla_hour > 1 (llegó tarde)
    empresas: int
    drivers: int


class AvailableDates(BaseModel):
    dates: list[str]
    min_date: Optional[str] = None
    max_date: Optional[str] = None


class SlaBin(BaseModel):
    bin_label: str
    bin_start: float
    count: int


class MotivoItem(BaseModel):
    motivo: str
    count: int


class EmpresaPerf(BaseModel):
    empresa_id: int
    nombre: str
    total: int
    completed: int
    failed: int
    ruta_anomala: int
    sla_hour_avg: float
    on_time_pct: float


class LocalidadPerf(BaseModel):
    localidad: str
    total: int
    failed: int
    failed_pct: float


class RutaAnomalaBreakdown(BaseModel):
    flag: str
    count: int
    pct: float


class VisitRow(BaseModel):
    id: int
    planned_date: str
    title: str
    order: int
    address: str
    status: str
    checkout_cl: Optional[str] = None
    current_eta_cl: Optional[str] = None
    sla_hour_checkout_eta: float
    ct: str
    drivername: str
    empresa_id: int
    ruta_anomala: bool
    am_pm: str


class VisitsPage(BaseModel):
    rows: list[VisitRow]
    total: int
    limit: int
    offset: int


# ---------- Endpoints ----------
@router.get("/available-dates", response_model=AvailableDates)
def available_dates(user: CurrentUser = Depends(current_user)) -> AvailableDates:
    scope_w, scope_p = _scope(user, "s")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT DISTINCT s.planned_date
            FROM fpoc.simpli_visits s
            WHERE 1=1 {scope_w}
            ORDER BY s.planned_date DESC
            """,
            *scope_p,
        )
        dates = [r[0].isoformat() for r in cur.fetchall()]
    return AvailableDates(
        dates=dates,
        min_date=dates[-1] if dates else None,
        max_date=dates[0] if dates else None,
    )


@router.get("/kpis", response_model=KPIs)
def kpis(
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> KPIs:
    where, params, resolved = _date_scope(user, planned_date, "s")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT
              COUNT(*)                                           AS total,
              SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN s.status='failed'    THEN 1 ELSE 0 END) AS failed,
              SUM(CAST(s.ruta_anomala AS INT))                   AS ruta_anomala,
              AVG(s.sla_hour_checkout_eta)                       AS sla_avg,
              SUM(CASE WHEN ABS(s.sla_hour_checkout_eta) <= 1 THEN 1 ELSE 0 END) AS on_time,
              SUM(CASE WHEN s.sla_hour_checkout_eta < -1 THEN 1 ELSE 0 END)       AS early,
              SUM(CASE WHEN s.sla_hour_checkout_eta >  1 THEN 1 ELSE 0 END)       AS late,
              COUNT(DISTINCT s.Empresa_falsa)                    AS empresas,
              COUNT(DISTINCT s.Drivername)                       AS drivers
            FROM fpoc.simpli_visits s
            WHERE 1=1 {where}
            """,
            *params,
        )
        r = cur.fetchone()

        # SQLite no tiene PERCENTILE_CONT; lo calculamos en Python.
        cur.execute(
            f"""
            SELECT s.sla_hour_checkout_eta
            FROM fpoc.simpli_visits s
            WHERE 1=1 {where}
            """,
            *params,
        )
        sla_vals = [float(row[0]) for row in cur.fetchall() if row[0] is not None]
        if sla_vals:
            import numpy as np
            p50 = float(np.percentile(sla_vals, 50))
            p90 = float(np.percentile(sla_vals, 90))
        else:
            p50, p90 = 0.0, 0.0
        p = type("P", (), {"p50": p50, "p90": p90})()

    total = int(r.total or 0)
    anom = int(r.ruta_anomala or 0)
    completed = int(r.completed or 0)
    return KPIs(
        planned_date=resolved,
        total=total,
        completed=completed,
        failed=int(r.failed or 0),
        completion_pct=round(100.0 * completed / max(1, total), 2),
        ruta_anomala=anom,
        ruta_anomala_pct=round(100.0 * anom / max(1, total), 2),
        sla_hour_avg=round(float(r.sla_avg or 0.0), 3),
        sla_hour_p50=round(float(p.p50 or 0.0), 3) if p else 0.0,
        sla_hour_p90=round(float(p.p90 or 0.0), 3) if p else 0.0,
        on_time=int(r.on_time or 0),
        early=int(r.early or 0),
        late=int(r.late or 0),
        empresas=int(r.empresas or 0),
        drivers=int(r.drivers or 0),
    )


@router.get("/sla-distribution", response_model=list[SlaBin])
def sla_distribution(
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> list[SlaBin]:
    where, params, _ = _date_scope(user, planned_date, "s")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT s.bin_label, MIN(s.bin_start) AS bin_start, COUNT(*) AS c
            FROM fpoc.simpli_visits s
            WHERE 1=1 {where}
            GROUP BY s.bin_label
            ORDER BY MIN(s.bin_start)
            """,
            *params,
        )
        return [
            SlaBin(bin_label=r.bin_label, bin_start=float(r.bin_start), count=int(r.c))
            for r in cur.fetchall()
        ]


@router.get("/motivos", response_model=list[MotivoItem])
def motivos(
    limit: int = Query(default=10, ge=1, le=50),
    user: CurrentUser = Depends(current_user),
) -> list[MotivoItem]:
    """Top motivos no entrega. Join geo_suborders -> simpli_visits via Empresa_falsa para scope."""
    # Scope via JOIN: geo_suborders tiene empresa_falsa (lowercase)
    if user.is_falabella:
        where, params = "", []
    else:
        where = " WHERE g.empresa_falsa = ?"
        params = [user.empresa_id]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT g.motivonoentrega AS motivo, COUNT(*) AS c
            FROM fpoc.geo_suborders g
            {where}
            {'AND' if where else 'WHERE'} g.motivonoentrega IS NOT NULL
            GROUP BY g.motivonoentrega
            ORDER BY COUNT(*) DESC
            LIMIT ?
            """,
            *params, limit,
        )
        return [MotivoItem(motivo=r.motivo, count=int(r.c)) for r in cur.fetchall()]


@router.get("/by-empresa", response_model=list[EmpresaPerf])
def by_empresa(
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> list[EmpresaPerf]:
    where, params, _ = _date_scope(user, planned_date, "s")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT s.Empresa_falsa AS empresa_id, e.nombre,
                   COUNT(*) AS total,
                   SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN s.status='failed'    THEN 1 ELSE 0 END) AS failed,
                   SUM(CAST(s.ruta_anomala AS INT)) AS anom,
                   AVG(s.sla_hour_checkout_eta) AS sla_avg,
                   SUM(CASE WHEN ABS(s.sla_hour_checkout_eta) <= 1 THEN 1 ELSE 0 END) AS on_time
            FROM fpoc.simpli_visits s
            LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = s.Empresa_falsa
            WHERE 1=1 {where}
            GROUP BY s.Empresa_falsa, e.nombre
            ORDER BY total DESC
            """,
            *params,
        )
        rows = cur.fetchall()
    out: list[EmpresaPerf] = []
    for r in rows:
        total = int(r.total or 0)
        out.append(EmpresaPerf(
            empresa_id=int(r.empresa_id),
            nombre=r.nombre or f"Transporte {int(r.empresa_id):02d}",
            total=total,
            completed=int(r.completed or 0),
            failed=int(r.failed or 0),
            ruta_anomala=int(r.anom or 0),
            sla_hour_avg=round(float(r.sla_avg or 0.0), 3),
            on_time_pct=round(100.0 * int(r.on_time or 0) / max(1, total), 2),
        ))
    return out


@router.get("/by-localidad", response_model=list[LocalidadPerf])
def by_localidad(
    limit: int = Query(default=15, ge=1, le=100),
    user: CurrentUser = Depends(current_user),
) -> list[LocalidadPerf]:
    if user.is_falabella:
        where, params = "", []
    else:
        where = " WHERE g.empresa_falsa = ?"
        params = [user.empresa_id]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT
              g.localidad,
              COUNT(*) AS total,
              SUM(CASE WHEN g.estado IN ('Pendiente','Planificado En Simpliroute') THEN 1 ELSE 0 END) AS failed
            FROM fpoc.geo_suborders g
            {where}
            GROUP BY g.localidad
            ORDER BY total DESC
            LIMIT ?
            """,
            *params, limit,
        )
        rows = cur.fetchall()
    out: list[LocalidadPerf] = []
    for r in rows:
        total = int(r.total or 0)
        failed = int(r.failed or 0)
        out.append(LocalidadPerf(
            localidad=r.localidad,
            total=total,
            failed=failed,
            failed_pct=round(100.0 * failed / max(1, total), 2),
        ))
    return out


@router.get("/rutas-anomalas", response_model=list[RutaAnomalaBreakdown])
def rutas_anomalas(
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> list[RutaAnomalaBreakdown]:
    where, params, _ = _date_scope(user, planned_date, "s")
    flags = [
        "ruta_eta_futuro",
        "ruta_fecha_inicio_mayor_eta",
        "ruta_primer_punto_lejano",
        "ruta_fecha_inicio_distinta_fecha_eta",
    ]
    select_parts = [f'SUM(CAST(s.{f} AS INTEGER)) AS "{f}"' for f in flags]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT COUNT(*) AS total, {', '.join(select_parts)}
            FROM fpoc.simpli_visits s
            WHERE 1=1 {where}
            """,
            *params,
        )
        r = cur.fetchone()
    total = int(r.total or 1)
    out: list[RutaAnomalaBreakdown] = []
    for f in flags:
        c = int(getattr(r, f) or 0)
        out.append(RutaAnomalaBreakdown(flag=f, count=c, pct=round(100.0 * c / total, 2)))
    return out


@router.get("/visits", response_model=VisitsPage)
def list_visits(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None),
    ruta_anomala: Optional[bool] = Query(default=None),
    empresa_id: Optional[int] = Query(default=None),
    localidad: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> VisitsPage:
    where_parts = ["1=1"]
    params: list = []
    if planned_date:
        where_parts.append("s.planned_date = ?")
        params.append(date_cls.fromisoformat(planned_date))
    scope_clause, scope_params = _scope(user, "s")
    if scope_clause:
        where_parts.append(scope_clause.lstrip(" AND"))
        params.extend(scope_params)
    if status:
        where_parts.append("s.status = ?")
        params.append(status)
    if ruta_anomala is not None:
        where_parts.append("s.ruta_anomala = ?")
        params.append(1 if ruta_anomala else 0)
    if empresa_id is not None and user.is_falabella:
        where_parts.append("s.Empresa_falsa = ?")
        params.append(empresa_id)
    if localidad:
        where_parts.append(
            "EXISTS (SELECT 1 FROM fpoc.geo_suborders g WHERE g.idruta IN "
            "(SELECT idruta FROM fpoc.geo_suborders WHERE fechainicioruta = s.Fechainicioruta) "
            "AND g.localidad = ?)"
        )
        params.append(localidad)
    if search:
        where_parts.append("(s.title LIKE ? OR s.address LIKE ? OR s.Drivername LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    where_sql = " AND ".join(where_parts)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM fpoc.simpli_visits s WHERE {where_sql}", *params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT s.id, s.planned_date, s.title, s."order", s.address, s.status,
                   s.checkout_cl, s.current_eta_cl, s.sla_hour_checkout_eta,
                   s.ct, s.Drivername, s.Empresa_falsa, s.ruta_anomala, s.am_pm
            FROM fpoc.simpli_visits s
            WHERE {where_sql}
            ORDER BY s.planned_date DESC, s.Empresa_falsa, s."order"
            LIMIT ? OFFSET ?
            """,
            *params, limit, offset,
        )
        rows = cur.fetchall()
    out_rows = [
        VisitRow(
            id=int(r.id),
            planned_date=r.planned_date.isoformat() if r.planned_date else "",
            title=r.title,
            order=int(r.order),
            address=r.address,
            status=r.status,
            checkout_cl=r.checkout_cl.isoformat() if r.checkout_cl else None,
            current_eta_cl=r.current_eta_cl.isoformat() if r.current_eta_cl else None,
            sla_hour_checkout_eta=float(r.sla_hour_checkout_eta or 0.0),
            ct=r.ct or "",
            drivername=r.Drivername or "",
            empresa_id=int(r.Empresa_falsa or 0),
            ruta_anomala=bool(r.ruta_anomala),
            am_pm=r.am_pm or "",
        )
        for r in rows
    ]
    return VisitsPage(rows=out_rows, total=total, limit=limit, offset=offset)

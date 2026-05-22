"""Dashboard stats live para el admin Falabella.

Endpoint GET /api/admin/day-stats?fecha=YYYY-MM-DD que devuelve KPIs del
día agregados para mostrar en widget de la web (Operación).

Incluye:
  - totals: completed / pending / failed / cancelled
  - por empresa: same breakdown
  - top atrasos: visitas pending con ETA vencida (orden por más vencida)
  - top motivos: motivos más frecuentes en visit_comments del día
  - intervenciones del día: count + últimas 5 (de visit_interventions)
  - completion ratio global y por empresa
  - driver con más entregas OK

Auth: admin/ops Falabella. Transport_manager puede usar el endpoint pero
solo ve datos de su empresa (filtro empresa_id).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn
from core.state import get_sim_clock


router = APIRouter(prefix="/api/admin", tags=["admin-day-stats"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DayStatsTotals(BaseModel):
    total: int
    completed: int
    pending: int
    failed: int
    cancelled: int
    completion_pct: float


class DayStatsByEmpresa(BaseModel):
    empresa_id: int
    empresa_nombre: str
    total: int
    completed: int
    pending: int
    failed: int
    cancelled: int
    completion_pct: float


class DayStatsAtrasoItem(BaseModel):
    tracking_id: str
    cliente: str
    comuna: Optional[str]
    driver_name: Optional[str]
    eta: Optional[str]
    minutes_late: int  # 0 si futura, positivo si vencida


class DayStatsMotivoItem(BaseModel):
    motivo: str
    count: int


class DayStatsInterventionItem(BaseModel):
    intervention_id: int
    tracking_id: str
    action: str
    admin_name: str
    reason: Optional[str]
    created_at: str


class DayStatsTopDriverItem(BaseModel):
    driver_id: str
    driver_name: str
    empresa_nombre: str
    completed: int
    total: int
    pct: float


class DayStatsResponse(BaseModel):
    fecha: str
    sim_clock: str
    totals: DayStatsTotals
    by_empresa: list[DayStatsByEmpresa]
    top_atrasos: list[DayStatsAtrasoItem]
    top_motivos: list[DayStatsMotivoItem]
    intervenciones_count: int
    intervenciones_recientes: list[DayStatsInterventionItem]
    top_driver: Optional[DayStatsTopDriverItem]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/day-stats", response_model=DayStatsResponse)
def day_stats(
    fecha: str = Query(..., description="YYYY-MM-DD"),
    user: CurrentUser = Depends(current_user),
) -> DayStatsResponse:
    """KPIs del día agregados para dashboard live."""
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"fecha invalida: {fecha!r}")

    sim_clock = get_sim_clock(fecha)
    empresa_filter = ""
    params_base: list = [fecha]
    if not user.is_falabella and user.empresa_id is not None:
        empresa_filter = " AND v.empresa_falsa = ?"
        params_base.append(user.empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()

        # 1) Totals globales (o de la empresa para transport_manager)
        cur.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN LOWER(status)='completed' THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN LOWER(status)='pending'   THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN LOWER(status)='failed'    THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN LOWER(status)='cancelled' THEN 1 ELSE 0 END) AS cancelled
            FROM fpoc.simpli_visits v
            WHERE v.planned_date = ?{empresa_filter}
            """,
            *params_base,
        )
        r = cur.fetchone()
        total = int(r[0] or 0)
        completed = int(r[1] or 0)
        pending = int(r[2] or 0)
        failed = int(r[3] or 0)
        cancelled = int(r[4] or 0)
        completion_pct = round(100.0 * completed / total, 1) if total > 0 else 0.0
        totals = DayStatsTotals(
            total=total, completed=completed, pending=pending,
            failed=failed, cancelled=cancelled,
            completion_pct=completion_pct,
        )

        # 2) Por empresa (solo si es falabella)
        by_empresa: list[DayStatsByEmpresa] = []
        if user.is_falabella:
            cur.execute(
                """
                SELECT e.empresa_id, COALESCE(e.nombre,'(sin nombre)') AS nombre,
                  COUNT(*) AS total,
                  SUM(CASE WHEN LOWER(v.status)='completed' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN LOWER(v.status)='pending'   THEN 1 ELSE 0 END) AS pend,
                  SUM(CASE WHEN LOWER(v.status)='failed'    THEN 1 ELSE 0 END) AS fail,
                  SUM(CASE WHEN LOWER(v.status)='cancelled' THEN 1 ELSE 0 END) AS canc
                FROM fpoc.simpli_visits v
                LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = v.empresa_falsa
                WHERE v.planned_date = ? AND v.empresa_falsa IS NOT NULL
                GROUP BY e.empresa_id, e.nombre
                ORDER BY done DESC, total DESC
                """,
                fecha,
            )
            for row in cur.fetchall():
                t = int(row[2] or 0)
                d = int(row[3] or 0)
                pct = round(100.0 * d / t, 1) if t > 0 else 0.0
                by_empresa.append(DayStatsByEmpresa(
                    empresa_id=int(row[0]), empresa_nombre=str(row[1]),
                    total=t, completed=d,
                    pending=int(row[4] or 0), failed=int(row[5] or 0),
                    cancelled=int(row[6] or 0), completion_pct=pct,
                ))

        # 3) Top atrasos (pending con ETA vencida)
        cur.execute(
            f"""
            SELECT TOP 10 v.id, v.title, v.comuna, v.driver_name, v.current_eta_cl
            FROM fpoc.simpli_visits v
            WHERE v.planned_date = ?
              AND LOWER(v.status) = 'pending'
              AND v.current_eta_cl IS NOT NULL
              AND v.current_eta_cl < ?{empresa_filter}
            ORDER BY v.current_eta_cl ASC
            """,
            *([fecha, sim_clock] + ([user.empresa_id] if not user.is_falabella and user.empresa_id is not None else []))
        )
        top_atrasos: list[DayStatsAtrasoItem] = []
        for row in cur.fetchall():
            eta = row[4]
            mins_late = 0
            if eta is not None:
                try:
                    delta = (sim_clock - eta).total_seconds() / 60
                    mins_late = max(0, int(delta))
                except Exception:  # noqa: BLE001
                    mins_late = 0
            top_atrasos.append(DayStatsAtrasoItem(
                tracking_id=str(row[0]),
                cliente=str(row[1] or "—"),
                comuna=str(row[2]) if row[2] else None,
                driver_name=str(row[3]) if row[3] else None,
                eta=eta.isoformat() if hasattr(eta, "isoformat") else (str(eta) if eta else None),
                minutes_late=mins_late,
            ))

        # 4) Top motivos del día
        params_motivos = [fecha]
        empresa_filter_c = ""
        if not user.is_falabella and user.empresa_id is not None:
            empresa_filter_c = " AND c.empresa_id = ?"
            params_motivos.append(user.empresa_id)
        cur.execute(
            f"""
            SELECT TOP 5 c.motivo, COUNT(*) AS n
            FROM fpoc.visit_comments c
            JOIN fpoc.simpli_visits v ON CAST(v.id AS VARCHAR(32)) = CAST(c.tracking_id AS VARCHAR(32))
            WHERE v.planned_date = ?{empresa_filter_c}
            GROUP BY c.motivo
            ORDER BY n DESC
            """,
            *params_motivos,
        )
        top_motivos = [
            DayStatsMotivoItem(motivo=str(r[0]), count=int(r[1]))
            for r in cur.fetchall()
        ]

        # 5) Intervenciones del día
        intervenciones_count = 0
        intervenciones_recientes: list[DayStatsInterventionItem] = []
        if user.is_falabella:
            try:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM fpoc.visit_interventions
                    WHERE CAST(created_at AS DATE) = ?
                    """,
                    fecha,
                )
                intervenciones_count = int(cur.fetchone()[0] or 0)

                cur.execute(
                    """
                    SELECT TOP 5 intervention_id, tracking_id, action,
                                 COALESCE(admin_name,''), reason, created_at
                    FROM fpoc.visit_interventions
                    WHERE CAST(created_at AS DATE) = ?
                    ORDER BY created_at DESC
                    """,
                    fecha,
                )
                for r in cur.fetchall():
                    intervenciones_recientes.append(DayStatsInterventionItem(
                        intervention_id=int(r[0]),
                        tracking_id=str(r[1]),
                        action=str(r[2]),
                        admin_name=str(r[3] or ""),
                        reason=str(r[4]) if r[4] else None,
                        created_at=r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5]),
                    ))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[day-stats] intervenciones query fallo: {e}")

        # 6) Driver con más completed del día
        top_driver: Optional[DayStatsTopDriverItem] = None
        cur.execute(
            f"""
            SELECT TOP 1 d.driver_id, d.name, COALESCE(e.nombre,''),
              SUM(CASE WHEN LOWER(v.status)='completed' THEN 1 ELSE 0 END) AS done,
              COUNT(*) AS total
            FROM fpoc.drivers d
            JOIN fpoc.simpli_visits v ON v.patente_falsa = d.vehicle_id
            LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id
            WHERE v.planned_date = ? AND d.active = 1{empresa_filter.replace('v.empresa_falsa','d.empresa_id')}
            GROUP BY d.driver_id, d.name, e.nombre
            HAVING SUM(CASE WHEN LOWER(v.status)='completed' THEN 1 ELSE 0 END) > 0
            ORDER BY done DESC, total ASC
            """,
            *params_base,
        )
        r = cur.fetchone()
        if r:
            t = int(r[4] or 0)
            d = int(r[3] or 0)
            top_driver = DayStatsTopDriverItem(
                driver_id=str(r[0]),
                driver_name=str(r[1] or "—"),
                empresa_nombre=str(r[2] or "—"),
                completed=d, total=t,
                pct=round(100.0 * d / t, 1) if t > 0 else 0.0,
            )

    return DayStatsResponse(
        fecha=fecha,
        sim_clock=sim_clock.isoformat(),
        totals=totals,
        by_empresa=by_empresa,
        top_atrasos=top_atrasos,
        top_motivos=top_motivos,
        intervenciones_count=intervenciones_count,
        intervenciones_recientes=intervenciones_recientes,
        top_driver=top_driver,
    )

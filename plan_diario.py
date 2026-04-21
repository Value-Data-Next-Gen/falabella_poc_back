"""Plan diario: vista jerárquica Empresa → Vehículo/Driver → Visitas ordenadas.

Combina:
  - STATE.snapshot_df (visitas del día con p_fallo, slack, ETA)
  - vehicle_empresa_map (mapping POC)
  - fpoc.visit_priority_overrides (si aplica)
  - fpoc.vip_clients (matchea por title)

Endpoint (GET /api/plan-diario):
  - empresa_id opcional (admin/ops puede filtrar; transport_manager fixed)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import CurrentUser, current_user
from db import get_conn
from state import STATE

router = APIRouter(prefix="/api/plan-diario", tags=["plan-diario"])


class PlanVisit(BaseModel):
    tracking_id: str
    order: int
    title: str
    address: str
    latitude: float
    longitude: float
    window_start: str
    window_end: str
    planned_arrival_time: str
    estimated_time_arrival: str
    slack_min: float
    alert_slack: str
    p_fallo: float
    status: str
    priority: str           # 'low' | 'normal' | 'high' | 'vip'
    priority_reason: Optional[str] = None
    is_vip: bool
    alert_valuedata: bool


class PlanDriver(BaseModel):
    vehicle_id: int
    vehicle_name: str
    driver_name: str
    total_visits: int
    completed: int
    pending: int
    red_visits: int
    vip_visits: int
    high_priority: int
    visits: list[PlanVisit]


class PlanEmpresa(BaseModel):
    empresa_id: int
    nombre: str
    total_visits: int
    completed: int
    pending: int
    red_visits: int
    vip_visits: int
    high_priority: int
    drivers: list[PlanDriver]


class PlanDiarioResponse(BaseModel):
    planned_date: str
    sim_clock: str
    empresas: list[PlanEmpresa]


@router.get("", response_model=PlanDiarioResponse)
def get_plan_diario(
    empresa_id: Optional[int] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> PlanDiarioResponse:
    if STATE.snapshot_df is None or STATE.today is None or STATE.sim_clock is None:
        raise HTTPException(503, "Backend warming up")

    # Scope
    if not user.is_falabella:
        empresa_id = user.empresa_id

    df = STATE.snapshot_df.copy()

    # Enriquecer con empresa_id y nombre
    df["empresa_id"] = df["vehicle_id"].astype(int).map(STATE.vehicle_empresa_map)
    if empresa_id is not None:
        df = df[df["empresa_id"] == empresa_id]

    # Join driver name from masters
    driver_by_vid = {int(v["vehicle_id"]): v["driver_name"] for v in STATE.vehicles_ext}

    # Cargar overrides de prioridad y VIP en bloque (una sola query)
    tids = df["tracking_id"].astype(str).unique().tolist()
    titles = df["title"].astype(str).unique().tolist()
    priority_map: dict[str, tuple[str, Optional[str]]] = {}
    vip_titles: set[str] = set()

    if tids or titles:
        with get_conn() as cn:
            cur = cn.cursor()
            if tids:
                marks = ",".join(["?"] * len(tids))
                cur.execute(
                    f"SELECT tracking_id, priority, reason FROM fpoc.visit_priority_overrides WHERE tracking_id IN ({marks})",
                    *tids,
                )
                for r in cur.fetchall():
                    priority_map[r.tracking_id] = (r.priority, r.reason)
            if titles:
                # Batch por 500 para evitar queries demasiado grandes
                for i in range(0, len(titles), 500):
                    batch = titles[i:i + 500]
                    marks = ",".join(["?"] * len(batch))
                    cur.execute(
                        f"""
                        SELECT DISTINCT match_value FROM fpoc.vip_clients
                        WHERE active = 1 AND match_type = 'title' AND match_value IN ({marks})
                        """,
                        *batch,
                    )
                    vip_titles.update(r.match_value for r in cur.fetchall())

    # Empresas catálogo
    empresas_catalog = {int(e["empresa_id"]): e["nombre"] for e in STATE.empresas}

    # Agrupar: empresa_id -> vehicle_id -> list of visits
    out_empresas: list[PlanEmpresa] = []
    for eid, edf in df.groupby("empresa_id"):
        drivers_out: list[PlanDriver] = []
        e_total = 0
        e_comp = 0
        e_pend = 0
        e_red = 0
        e_vip = 0
        e_high = 0
        for vid, vdf in edf.groupby("vehicle_id"):
            vdf_sorted = vdf.sort_values("order")
            visits_out: list[PlanVisit] = []
            v_red = 0
            v_vip_count = 0
            v_high = 0
            for _, row in vdf_sorted.iterrows():
                tid = str(row["tracking_id"])
                title = str(row["title"])
                prio, reason = priority_map.get(tid, ("normal", None))
                is_vip = title in vip_titles
                # Si es VIP por cliente, la prioridad efectiva sube a 'vip' si no hay override superior
                if is_vip and prio in ("normal", "low"):
                    prio = "vip"
                if prio == "vip":
                    v_vip_count += 1
                if prio == "high":
                    v_high += 1
                if str(row.get("alert_slack", "")) == "RED" and row["status"] == "pending":
                    v_red += 1
                visits_out.append(PlanVisit(
                    tracking_id=tid,
                    order=int(row["order"]),
                    title=title,
                    address=str(row["address"]),
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    window_start=str(row["window_start"]),
                    window_end=str(row["window_end"]),
                    planned_arrival_time=str(row["planned_arrival_time"]),
                    estimated_time_arrival=str(row["estimated_time_arrival"]),
                    slack_min=float(row["slack_min"]),
                    alert_slack=str(row["alert_slack"]),
                    p_fallo=float(row["p_fallo"]),
                    status=str(row["status"]),
                    priority=prio,
                    priority_reason=reason,
                    is_vip=is_vip,
                    alert_valuedata=bool(row["alert_valuedata"]),
                ))
            total = int(len(vdf_sorted))
            comp = int((vdf_sorted["status"] == "completed").sum())
            pend = total - comp
            drivers_out.append(PlanDriver(
                vehicle_id=int(vid),
                vehicle_name=str(vdf_sorted["vehicle_name"].iloc[0]),
                driver_name=driver_by_vid.get(int(vid), f"Driver {vid}"),
                total_visits=total,
                completed=comp,
                pending=pend,
                red_visits=v_red,
                vip_visits=v_vip_count,
                high_priority=v_high,
                visits=visits_out,
            ))
            e_total += total
            e_comp += comp
            e_pend += pend
            e_red += v_red
            e_vip += v_vip_count
            e_high += v_high

        drivers_out.sort(key=lambda d: d.vehicle_id)
        out_empresas.append(PlanEmpresa(
            empresa_id=int(eid),
            nombre=empresas_catalog.get(int(eid), f"Empresa {eid}"),
            total_visits=e_total,
            completed=e_comp,
            pending=e_pend,
            red_visits=e_red,
            vip_visits=e_vip,
            high_priority=e_high,
            drivers=drivers_out,
        ))

    out_empresas.sort(key=lambda e: e.empresa_id)

    return PlanDiarioResponse(
        planned_date=STATE.today.isoformat(),
        sim_clock=STATE.sim_clock.isoformat(),
        empresas=out_empresas,
    )

"""ValueData backend (FastAPI).

Capa de prediccion anticipada que se monta encima de SimpliRoute. En este POC
genera el plan localmente con la misma forma que devolveria SimpliRoute; en
produccion `pipeline.gen_today_plan` se reemplaza por una llamada a la API real
(ver `_load_today_from_simpliroute` stub).

Endpoints (prefijo /api):
  GET  /state
  GET  /kpis
  GET  /visits
  GET  /alerts/anticipated
  GET  /visits/{tracking_id}/explanation
  GET  /vehicles
  GET  /model/metrics
  GET  /model/importance
  POST /control/incident
  POST /control/reset
  POST /control/clock
  GET  /health
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

import numpy as np
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

# Cargar .env antes de importar state/auth (que leen DB_*)
for _p in (Path(__file__).resolve().parent / ".env",
           Path(__file__).resolve().parent.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from pipeline import (
    ANTICIPATION_HOURS,
    PRICE_PER_RESCUE_CLP,
    RESCUE_RATE,
    humanize_feature,
    top_shap_factors,
)
from events import EVENTS
from schemas import (
    AnticipatedAlert,
    ClientMaster,
    ClockRequest,
    Driver,
    FeatureImportance,
    IncidentRequest,
    KPIs,
    ModelMetrics,
    ShapFactor,
    StateResponse,
    StreamEvent,
    VehicleExtended,
    VehicleSummary,
    Visit,
    VisitExplanation,
)
from state import STATE
from auth import (
    CurrentUser,
    current_user,
    empresas_router,
    router as auth_router,
)

logger.remove()
logger.add(sys.stderr, level="INFO")

SCHEDULER_TICK_SEC = 3  # cada 3s avanza sim_clock por sim_minutes_per_tick


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Bootstrapping ValueData backend (training model, may take 30-40s)...")
    STATE.init()
    logger.info(
        f"Model ready. AUC={STATE.boot['metrics']['auc']:.3f}, "
        f"Brier={STATE.boot['metrics']['brier']:.4f}. "
        f"Today plan: {len(STATE.today_plan)} visits."
    )

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        STATE.tick, "interval",
        seconds=SCHEDULER_TICK_SEC, id="sim-tick",
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started: tick every {SCHEDULER_TICK_SEC}s")

    # Live SQL generator (inserta rows aleatorias en fpoc.simpli_visits)
    live_gen_start()

    # Simulador de comentarios alertables (off por default; se enciende por endpoint)
    comment_sim_start()

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        live_gen_stop()
        comment_sim_stop()


app = FastAPI(
    title="ValueData backend - Torre de Control",
    version="0.1.0",
    lifespan=lifespan,
)

_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from seguimiento import router as seguimiento_router
from notifications import router as notifications_router
from preferences import router as preferences_router
from vip import router as vip_router
from priorities import router as priorities_router
from plan_diario import router as plan_diario_router
from watchlist import router as watchlist_router
from live_generator import (
    router as live_gen_router,
    start_scheduler as live_gen_start,
    stop_scheduler as live_gen_stop,
)
from mantenedores import router as mantenedores_router
from comments import router as comments_router
from motivo_classifier import router as motivo_classifier_router
from comment_simulator import (
    router as comment_sim_router,
    start_scheduler as comment_sim_start,
    stop_scheduler as comment_sim_stop,
)

app.include_router(auth_router)
app.include_router(empresas_router)
app.include_router(seguimiento_router)
app.include_router(notifications_router)
app.include_router(preferences_router)
app.include_router(vip_router)
app.include_router(priorities_router)
app.include_router(plan_diario_router)
app.include_router(watchlist_router)
app.include_router(live_gen_router)
app.include_router(mantenedores_router)
app.include_router(comments_router)
app.include_router(comment_sim_router)
app.include_router(motivo_classifier_router)


def _scope_df(df, user: CurrentUser):
    """Filtra el df a los vehicles de la empresa del user (transport_manager).
    Los usuarios falabella_* ven todo."""
    if user.is_falabella:
        return df
    allowed = set(STATE.vehicle_ids_for_empresa(user.empresa_id))
    return df[df["vehicle_id"].isin(allowed)]


# =============================================================================
# Helpers
# =============================================================================
def _require_ready() -> None:
    if STATE.boot is None or STATE.snapshot_df is None:
        raise HTTPException(status_code=503, detail="Backend warming up, try again in a few seconds")


def _df_to_visits(df) -> list[Visit]:
    return [
        Visit(
            tracking_id=str(row["tracking_id"]),
            vehicle_id=int(row["vehicle_id"]),
            vehicle_name=str(row["vehicle_name"]),
            order=int(row["order"]),
            title=str(row["title"]),
            address=str(row["address"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            load=float(row["load"]),
            window_start=str(row["window_start"]),
            window_end=str(row["window_end"]),
            planned_arrival_time=str(row["planned_arrival_time"]),
            estimated_time_arrival=str(row["estimated_time_arrival"]),
            slack_min=float(row["slack_min"]),
            alert_slack=str(row["alert_slack"]),
            p_fallo=float(row["p_fallo"]),
            alert_valuedata=bool(row["alert_valuedata"]),
            status=str(row["status"]),
            horas_hasta_window_end=float(row["horas_hasta_we"]),
        )
        for _, row in df.iterrows()
    ]


# =============================================================================
# Endpoints
# =============================================================================
@app.get("/api/health")
def health():
    return {"status": "ok", "ready": STATE.boot is not None}


@app.get("/api/state", response_model=StateResponse)
def get_state(user: CurrentUser = Depends(current_user)):
    _require_ready()
    df = _scope_df(STATE.snapshot_df, user)
    return StateResponse(
        sim_clock=STATE.sim_clock,
        today=STATE.today.isoformat(),
        day_seed=STATE.day_seed,
        auto_advance=STATE.auto_advance,
        sim_minutes_per_tick=STATE.sim_minutes_per_tick,
        total_visits=int(len(df)),
        vehicles=sorted(int(v) for v in df["vehicle_id"].unique()),
        incidents={int(k): float(v) for k, v in STATE.manual_incidents.items()},
        last_tick_at=STATE.last_tick_at,
    )


@app.get("/api/kpis", response_model=KPIs)
def get_kpis(
    vehicle_id: list[int] | None = Query(default=None),
    user: CurrentUser = Depends(current_user),
):
    _require_ready()
    df = _scope_df(STATE.snapshot_df, user)
    if vehicle_id:
        df = df[df["vehicle_id"].isin(vehicle_id)]
    total = int(len(df))
    completed = int((df["status"] == "completed").sum())
    pending = total - completed
    pending_df = df[df["status"] == "pending"]
    red = int((pending_df["alert_slack"] == "RED").sum())
    yellow = int((pending_df["alert_slack"] == "YELLOW").sum())
    vd_alerts = int(df["alert_valuedata"].sum())
    real_fails = int(df["failed"].sum()) if "failed" in df.columns else 0
    vd_caught_real = int((df["alert_valuedata"] & (df["failed"] == 1)).sum()) if "failed" in df.columns else 0
    expected_fail = float(df["p_fallo"].sum())
    saved = vd_alerts * RESCUE_RATE
    proj = 100.0 * (1.0 - max(0.0, expected_fail - saved) / max(1, total))
    return KPIs(
        total=total,
        completed=completed,
        in_route=0,
        pending=pending,
        red_simpliroute=red,
        yellow_simpliroute=yellow,
        vd_alerts=vd_alerts,
        vd_alerts_caught_real=vd_caught_real,
        real_failures_oracle=real_fails,
        projected_compliance_pct=float(round(proj, 2)),
        rescue_clp=int(round(saved * PRICE_PER_RESCUE_CLP)),
    )


@app.get("/api/visits", response_model=list[Visit])
def get_visits(
    vehicle_id: list[int] | None = Query(default=None),
    status: str | None = Query(default=None),
    only_alerts: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
):
    _require_ready()
    df = _scope_df(STATE.snapshot_df, user)
    if vehicle_id:
        df = df[df["vehicle_id"].isin(vehicle_id)]
    if status:
        df = df[df["status"] == status]
    if only_alerts:
        df = df[df["alert_valuedata"] | (df["alert_slack"] != "GREEN")]
    return _df_to_visits(df)


@app.get("/api/alerts/anticipated", response_model=list[AnticipatedAlert])
def get_anticipated_alerts(
    limit: int = Query(default=20, ge=1, le=200),
    user: CurrentUser = Depends(current_user),
):
    _require_ready()
    df = _scope_df(STATE.snapshot_df, user)
    shap_vals = STATE.shap_vals
    feats = STATE.boot["feature_names"]
    alerts = df[df["alert_valuedata"]].sort_values("p_fallo", ascending=False).head(limit)
    out: list[AnticipatedAlert] = []
    for _, row in alerts.iterrows():
        idx = int(row["_shap_idx"])
        tops = top_shap_factors(shap_vals, feats, idx, k=3, only_positive=True)
        factors = [
            ShapFactor(name=n, display=humanize_feature(n), contribution=float(v))
            for n, v in tops
        ]
        out.append(AnticipatedAlert(
            tracking_id=str(row["tracking_id"]),
            title=str(row["title"]),
            vehicle_id=int(row["vehicle_id"]),
            vehicle_name=str(row["vehicle_name"]),
            window_end=str(row["window_end"]),
            estimated_time_arrival=str(row["estimated_time_arrival"]),
            p_fallo=float(row["p_fallo"]),
            horas_hasta_window_end=float(row["horas_hasta_we"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            top_factors=factors,
        ))
    return out


@app.get("/api/visits/{tracking_id}/explanation", response_model=VisitExplanation)
def get_explanation(tracking_id: str, user: CurrentUser = Depends(current_user)):
    _require_ready()
    df = _scope_df(STATE.snapshot_df, user)
    matching = df[df["tracking_id"] == tracking_id]
    if matching.empty:
        raise HTTPException(status_code=404, detail=f"tracking_id {tracking_id} not found")
    row = matching.iloc[0]
    idx = int(row["_shap_idx"])
    tops = top_shap_factors(STATE.shap_vals, STATE.boot["feature_names"], idx, k=5, only_positive=False)
    factors = [
        ShapFactor(name=n, display=humanize_feature(n), contribution=float(v))
        for n, v in tops
    ]
    return VisitExplanation(
        tracking_id=str(row["tracking_id"]),
        title=str(row["title"]),
        p_fallo=float(row["p_fallo"]),
        alert_slack=str(row["alert_slack"]),
        alert_valuedata=bool(row["alert_valuedata"]),
        top_factors=factors,
    )


@app.get("/api/vehicles", response_model=list[VehicleSummary])
def get_vehicles(user: CurrentUser = Depends(current_user)):
    _require_ready()
    df = _scope_df(STATE.snapshot_df, user)
    out: list[VehicleSummary] = []
    for v_id, vdf in df.groupby("vehicle_id"):
        last_obs = float(vdf["_obs_delay"].iloc[0]) if "_obs_delay" in vdf.columns else 0.0
        out.append(VehicleSummary(
            vehicle_id=int(v_id),
            vehicle_name=str(vdf["vehicle_name"].iloc[0]),
            n_visits=int(len(vdf)),
            completed=int((vdf["status"] == "completed").sum()),
            pending=int((vdf["status"] == "pending").sum()),
            red_simpliroute=int(((vdf["status"] == "pending") & (vdf["alert_slack"] == "RED")).sum()),
            vd_alerts=int(vdf["alert_valuedata"].sum()),
            last_observed_delay_min=float(round(last_obs, 1)),
            incident_extra_min=float(STATE.manual_incidents.get(int(v_id), 0.0)),
        ))
    out.sort(key=lambda x: x.vehicle_id)
    return out


@app.get("/api/model/metrics", response_model=ModelMetrics)
def get_model_metrics():
    _require_ready()
    m = STATE.boot["metrics"]
    return ModelMetrics(
        auc=m["auc"],
        brier=m["brier"],
        confusion_matrix=m["confusion_matrix"],
        calibration_curve=m["calibration_curve"],
        n_train=m["n_train"],
        n_val=m["n_val"],
        base_rate_train=m["base_rate_train"],
        base_rate_val=m["base_rate_val"],
    )


@app.get("/api/model/importance", response_model=list[FeatureImportance])
def get_model_importance(top_k: int = Query(default=15, ge=1, le=50)):
    _require_ready()
    feats = STATE.boot["feature_names"]
    importances = np.abs(STATE.shap_vals).mean(axis=0)
    pairs = list(zip(feats, importances))
    pairs.sort(key=lambda x: x[1], reverse=True)
    pairs = pairs[:top_k]
    return [
        FeatureImportance(name=n, display=humanize_feature(n), importance=float(v))
        for n, v in pairs
    ]


@app.post("/api/control/incident")
def post_incident(req: IncidentRequest, user: CurrentUser = Depends(current_user)):
    _require_ready()
    # transport_manager solo puede inyectar incidentes a sus propios vehículos
    if not user.is_falabella:
        allowed = set(STATE.vehicle_ids_for_empresa(user.empresa_id))
        if req.vehicle_id not in allowed:
            raise HTTPException(status_code=403, detail="vehicle fuera de tu empresa")
    STATE.add_incident(req.vehicle_id, req.extra_min)
    return {"status": "ok", "incidents": STATE.manual_incidents}


class ResetRequest(BaseModel):
    start_date: Optional[str] = None
    day_seed: Optional[int] = None
    sim_minutes_per_tick: Optional[int] = Field(default=None, ge=1, le=120)


@app.post("/api/control/reset")
def post_reset(req: ResetRequest | None = None, user: CurrentUser = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="solo admin puede resetear")
    _require_ready()
    start_date = None
    if req and req.start_date:
        from datetime import date as _date
        start_date = _date.fromisoformat(req.start_date)
    STATE.reset_day(start_date=start_date, day_seed=req.day_seed if req else None)
    if req and req.sim_minutes_per_tick is not None:
        STATE.set_sim_minutes_per_tick(req.sim_minutes_per_tick)
    return {
        "status": "ok",
        "today": STATE.today.isoformat() if STATE.today else None,
        "day_seed": STATE.day_seed,
        "sim_clock": STATE.sim_clock.isoformat(),
        "sim_minutes_per_tick": STATE.sim_minutes_per_tick,
    }


class StartDayRequest(BaseModel):
    regen_plan: bool = False
    day_seed: Optional[int] = None


@app.post("/api/control/freeze")
def post_freeze(user: CurrentUser = Depends(current_user)):
    """Congela el día: setea sim_clock al inicio (09:00) y pausa auto_advance.
    Ideal para pre-configurar prioridades antes de arrancar."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="solo admin puede congelar el día")
    _require_ready()
    from datetime import time as _time, datetime as _dt
    day_start_dt = _dt.combine(STATE.today, _time(9, 0))  # type: ignore[arg-type]
    STATE.set_clock(sim_clock=day_start_dt)
    STATE.set_auto_advance(False)
    return {
        "status": "frozen",
        "sim_clock": STATE.sim_clock.isoformat(),
        "auto_advance": STATE.auto_advance,
    }


@app.post("/api/control/start-day")
def post_start_day(req: StartDayRequest | None = None,
                    user: CurrentUser = Depends(current_user)):
    """Arranca el día: opcionalmente regenera el plan y resetea sim_clock al
    inicio, luego activa auto_advance."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="solo admin puede iniciar el día")
    _require_ready()
    if req and req.regen_plan:
        STATE.reset_day(day_seed=req.day_seed)
    from datetime import time as _time, datetime as _dt
    day_start_dt = _dt.combine(STATE.today, _time(9, 0))  # type: ignore[arg-type]
    if STATE.sim_clock and STATE.sim_clock < day_start_dt:
        STATE.set_clock(sim_clock=day_start_dt)
    STATE.set_auto_advance(True)
    return {
        "status": "running",
        "today": STATE.today.isoformat() if STATE.today else None,
        "day_seed": STATE.day_seed,
        "sim_clock": STATE.sim_clock.isoformat(),
        "auto_advance": STATE.auto_advance,
    }


@app.post("/api/control/clock")
def post_clock(req: ClockRequest, user: CurrentUser = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="solo admin puede cambiar el reloj")
    _require_ready()
    STATE.set_clock(sim_clock=req.sim_clock, offset_minutes=req.offset_minutes)
    if req.auto_advance is not None:
        STATE.set_auto_advance(req.auto_advance)
    return {
        "status": "ok",
        "sim_clock": STATE.sim_clock.isoformat(),
        "auto_advance": STATE.auto_advance,
    }


# =============================================================================
# Maestros (estilo SimpliRoute)
# =============================================================================
@app.get("/api/drivers", response_model=list[Driver])
def get_drivers():
    _require_ready()
    return STATE.drivers


@app.get("/api/drivers/{driver_id}", response_model=Driver)
def get_driver(driver_id: str):
    _require_ready()
    for d in STATE.drivers:
        if d["driver_id"] == driver_id:
            return d
    raise HTTPException(status_code=404, detail=f"driver {driver_id} not found")


@app.get("/api/fleet/vehicles", response_model=list[VehicleExtended])
def get_fleet_vehicles():
    _require_ready()
    return STATE.vehicles_ext


@app.get("/api/fleet/vehicles/{vehicle_id}", response_model=VehicleExtended)
def get_fleet_vehicle(vehicle_id: int):
    _require_ready()
    for v in STATE.vehicles_ext:
        if v["vehicle_id"] == vehicle_id:
            return v
    raise HTTPException(status_code=404, detail=f"vehicle {vehicle_id} not found")


@app.get("/api/clients", response_model=list[ClientMaster])
def get_clients(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    only_problem_zone: bool = Query(default=False),
    min_fail_rate: float = Query(default=0.0, ge=0.0, le=1.0),
    search: str | None = Query(default=None),
):
    _require_ready()
    items = STATE.clients_master
    if only_problem_zone:
        items = [c for c in items if c["is_problem_zone"]]
    if min_fail_rate > 0:
        items = [c for c in items if c["fail_rate_60d"] >= min_fail_rate]
    if search:
        s = search.lower()
        items = [c for c in items if s in c["title"].lower() or s in c["customer_id"].lower()]
    return items[offset:offset + limit]


@app.get("/api/clients/{customer_id}", response_model=ClientMaster)
def get_client(customer_id: str):
    _require_ready()
    for c in STATE.clients_master:
        if c["customer_id"] == customer_id:
            return c
    raise HTTPException(status_code=404, detail=f"client {customer_id} not found")


# =============================================================================
# Stream de eventos en vivo
# =============================================================================
@app.get("/api/events/stream", response_model=list[StreamEvent])
def get_events(
    limit: int = Query(default=50, ge=1, le=200),
    types: list[str] | None = Query(default=None),
):
    return EVENTS.recent(limit=limit, types=types)

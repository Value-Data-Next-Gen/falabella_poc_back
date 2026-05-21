"""Endpoints "legacy" sobrevivientes tras eliminar ML (Fase 2 MVP refactor).

Tras el refactor, los routers `model_router` y `control_router` que dependían
del modelo XGB + SHAP + STATE.snapshot_df fueron eliminados:

  Removidos:
    GET  /api/kpis
    GET  /api/visits
    GET  /api/alerts/anticipated
    GET  /api/visits/{tid}/explanation
    GET  /api/vehicles
    GET  /api/model/metrics
    GET  /api/model/importance
    POST /api/control/start-day
    GET  /api/clients
    GET  /api/clients/{customer_id}

Sobreviven dos sub-routers:

  - `system_router` : /api/health, /api/state, /api/admin/config (GET/PUT)
  - `fleet_router`  : /api/drivers, /api/drivers/{id}, /api/fleet/vehicles,
                      /api/fleet/vehicles/{id}, /api/events/stream
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user, require_admin
from core.events import EVENTS
from core.schemas import (
    Driver,
    StateResponse,
    StreamEvent,
    VehicleExtended,
)
from core.state import STATE


# =============================================================================
# system_router — health, state, admin/config
# =============================================================================
system_router = APIRouter(tags=["system"])


@system_router.get("/api/health")
def health():
    return {"status": "ok", "ready": True}


class AppConfigEntry(BaseModel):
    value: float
    updated_at: Optional[str] = None
    updated_by_user_id: Optional[int] = None


class AppConfigResponse(BaseModel):
    eta_window_hours: AppConfigEntry
    alert_threshold: AppConfigEntry


class AppConfigUpdate(BaseModel):
    eta_window_hours: Optional[float] = Field(default=None, ge=0.0, le=24.0)
    alert_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)


@system_router.get("/api/admin/config", response_model=AppConfigResponse)
def get_app_config(user: CurrentUser = Depends(require_admin)):
    import core.app_config as _cfg
    meta = _cfg.get_audit_meta()
    return AppConfigResponse(
        eta_window_hours=AppConfigEntry(**meta["eta_window_hours"]),
        alert_threshold=AppConfigEntry(**meta["alert_threshold"]),
    )


@system_router.put("/api/admin/config", response_model=AppConfigResponse)
def update_app_config(
    body: AppConfigUpdate,
    user: CurrentUser = Depends(require_admin),
):
    import core.app_config as _cfg
    if body.eta_window_hours is not None:
        _cfg.set_eta_window_hours(body.eta_window_hours, user_id=user.user_id)
    if body.alert_threshold is not None:
        _cfg.set_alert_threshold(body.alert_threshold, user_id=user.user_id)
    meta = _cfg.get_audit_meta()
    return AppConfigResponse(
        eta_window_hours=AppConfigEntry(**meta["eta_window_hours"]),
        alert_threshold=AppConfigEntry(**meta["alert_threshold"]),
    )


@system_router.get("/api/state", response_model=StateResponse)
def get_state(user: CurrentUser = Depends(current_user)):
    """Estado mínimo de la torre.

    Tras Fase 2 MVP: sin snapshot ML, sin sim_clock auto-advance. Los campos
    obsoletos quedan con defaults estables (day_seed=0, auto_advance=False,
    sim_minutes_per_tick=0, incidents={}, last_tick_at=None) para no romper
    el contrato con el frontend mientras se migra.

    `vehicles` y `total_visits` se computan leyendo fpoc.simpli_visits para
    la fecha activa (STATE.today). Si STATE.today es None o falla, se cae a 0.
    """
    from core.db import get_conn

    today = STATE.today
    today_iso = today.isoformat() if today is not None else ""
    sim_clock = STATE.sim_clock or datetime.utcnow()

    vehicles: list[int] = []
    total_visits = 0
    if today is not None:
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                if user.is_falabella:
                    cur.execute(
                        "SELECT DISTINCT patente_falsa FROM fpoc.simpli_visits "
                        "WHERE planned_date = ? AND patente_falsa IS NOT NULL",
                        today_iso,
                    )
                    vehicles = sorted(int(r[0]) for r in cur.fetchall())
                    cur.execute(
                        "SELECT COUNT(*) FROM fpoc.simpli_visits WHERE planned_date = ?",
                        today_iso,
                    )
                    total_visits = int(cur.fetchone()[0] or 0)
                else:
                    cur.execute(
                        "SELECT DISTINCT patente_falsa FROM fpoc.simpli_visits "
                        "WHERE planned_date = ? AND patente_falsa IS NOT NULL "
                        "AND empresa_falsa = ?",
                        today_iso, user.empresa_id,
                    )
                    vehicles = sorted(int(r[0]) for r in cur.fetchall())
                    cur.execute(
                        "SELECT COUNT(*) FROM fpoc.simpli_visits "
                        "WHERE planned_date = ? AND empresa_falsa = ?",
                        today_iso, user.empresa_id,
                    )
                    total_visits = int(cur.fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            pass

    return StateResponse(
        sim_clock=sim_clock,
        today=today_iso,
        day_seed=0,
        auto_advance=False,
        sim_minutes_per_tick=0,
        total_visits=total_visits,
        vehicles=vehicles,
        incidents={},
        last_tick_at=None,
    )


# =============================================================================
# fleet_router — drivers, fleet/vehicles, events/stream
# =============================================================================
fleet_router = APIRouter(tags=["fleet"])


@fleet_router.get("/api/drivers", response_model=list[Driver])
def get_drivers(user: CurrentUser = Depends(current_user)):
    if user.is_falabella:
        return STATE.drivers
    return [d for d in STATE.drivers if d.get("empresa_id") == user.empresa_id]


@fleet_router.get("/api/drivers/{driver_id}", response_model=Driver)
def get_driver(driver_id: str, user: CurrentUser = Depends(current_user)):
    for d in STATE.drivers:
        if d["driver_id"] == driver_id:
            if not user.is_falabella and d.get("empresa_id") != user.empresa_id:
                raise HTTPException(status_code=404, detail=f"driver {driver_id} not found")
            return d
    raise HTTPException(status_code=404, detail=f"driver {driver_id} not found")


@fleet_router.get("/api/fleet/vehicles", response_model=list[VehicleExtended])
def get_fleet_vehicles():
    return STATE.vehicles_ext


@fleet_router.get("/api/fleet/vehicles/{vehicle_id}", response_model=VehicleExtended)
def get_fleet_vehicle(vehicle_id: int):
    for v in STATE.vehicles_ext:
        if v["vehicle_id"] == vehicle_id:
            return v
    raise HTTPException(status_code=404, detail=f"vehicle {vehicle_id} not found")


@fleet_router.get("/api/events/stream", response_model=list[StreamEvent])
def get_events(
    limit: int = Query(default=50, ge=1, le=200),
    types: list[str] | None = Query(default=None),
):
    return EVENTS.recent(limit=limit, types=types)

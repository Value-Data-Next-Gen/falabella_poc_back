"""Pydantic schemas para responses (contrato con el frontend TypeScript)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class StateResponse(BaseModel):
    sim_clock: datetime
    today: str
    day_seed: int
    auto_advance: bool
    sim_minutes_per_tick: int
    total_visits: int
    vehicles: list[int]
    incidents: dict[int, float]
    last_tick_at: Optional[datetime] = None


class KPIs(BaseModel):
    total: int
    completed: int
    in_route: int
    pending: int
    red_simpliroute: int
    yellow_simpliroute: int
    vd_alerts: int
    vd_alerts_caught_real: int  # alertas anticipadas que efectivamente fallarian
    real_failures_oracle: int   # cuantas fallarian sin intervenir (oracle, solo demo)
    projected_compliance_pct: float
    rescue_clp: int


class Visit(BaseModel):
    tracking_id: str
    vehicle_id: int
    vehicle_name: str
    order: int
    title: str
    address: str
    latitude: float
    longitude: float
    load: float
    window_start: str
    window_end: str
    planned_arrival_time: str
    estimated_time_arrival: str
    slack_min: float
    alert_slack: str
    p_fallo: float
    alert_valuedata: bool
    status: str
    horas_hasta_window_end: float


class ShapFactor(BaseModel):
    name: str
    display: str
    contribution: float


class AnticipatedAlert(BaseModel):
    tracking_id: str
    title: str
    vehicle_id: int
    vehicle_name: str
    window_end: str
    estimated_time_arrival: str
    p_fallo: float
    horas_hasta_window_end: float
    latitude: float
    longitude: float
    top_factors: list[ShapFactor]


class VisitExplanation(BaseModel):
    tracking_id: str
    title: str
    p_fallo: float
    alert_slack: str
    alert_valuedata: bool
    top_factors: list[ShapFactor]


class CalibrationPoint(BaseModel):
    predicted: float
    actual: float


class ModelMetrics(BaseModel):
    auc: float
    brier: float
    confusion_matrix: list[list[int]]
    calibration_curve: list[CalibrationPoint]
    n_train: int
    n_val: int
    base_rate_train: float
    base_rate_val: float


class FeatureImportance(BaseModel):
    name: str
    display: str
    importance: float


class VehicleSummary(BaseModel):
    vehicle_id: int
    vehicle_name: str
    n_visits: int
    completed: int
    pending: int
    red_simpliroute: int
    vd_alerts: int
    last_observed_delay_min: float
    incident_extra_min: float


class IncidentRequest(BaseModel):
    vehicle_id: int
    extra_min: float = Field(gt=0, le=240)


class ClockRequest(BaseModel):
    sim_clock: Optional[datetime] = None
    offset_minutes: Optional[int] = None
    auto_advance: Optional[bool] = None


# ---- Maestros ----
class Driver(BaseModel):
    driver_id: str
    name: str
    phone: str
    license: str
    vehicle_id: int
    vehicle_name: str
    rating: float
    deliveries_30d: int
    fail_rate_30d: float
    active: bool
    joined_at: str


class VehicleExtended(BaseModel):
    vehicle_id: int
    name: str
    type: str
    plate: str
    capacity_m3: int
    driver_id: str
    driver_name: str
    depot_lat: float
    depot_lon: float
    active: bool
    year: int


class ClientMaster(BaseModel):
    customer_id: str
    title: str
    address: str
    latitude: float
    longitude: float
    comuna_id: str
    is_problem_zone: bool
    n_visits_60d: int
    n_failed_60d: int
    fail_rate_60d: float
    first_seen: str
    last_seen: str


# ---- Eventos ----
class StreamEvent(BaseModel):
    event_id: str
    type: str
    sim_ts: datetime
    wall_ts: datetime
    # campos opcionales segun tipo
    tracking_id: Optional[str] = None
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    title: Optional[str] = None
    window_end: Optional[str] = None
    eta: Optional[str] = None
    slack_min: Optional[float] = None
    delay_min: Optional[float] = None
    p_fallo: Optional[float] = None
    horas_hasta_we: Optional[float] = None
    extra_min: Optional[float] = None
    reason: Optional[str] = None
    new_day_seed: Optional[int] = None
    motivo: Optional[str] = None
    comentario: Optional[str] = None
    severity: Optional[str] = None
    reported_by: Optional[str] = None

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
    n_subordenes: int = 1
    region: Optional[str] = None
    comuna: Optional[str] = None
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
    empresa_id: Optional[int] = None
    vehicle_id: int
    vehicle_name: str
    rating: float
    deliveries_30d: int
    fail_rate_30d: float
    active: bool
    joined_at: str


class VehicleExtended(BaseModel):
    vehicle_id: int
    empresa_id: Optional[int] = None
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
    # Sprint 4.A2: motivo_correction_suggested / motivo_correction_decided
    correction_id: Optional[int] = None
    comment_id: Optional[int] = None
    motivo_reportado: Optional[str] = None
    motivo_sugerido: Optional[str] = None
    motivo_aplicado: Optional[str] = None
    confianza: Optional[str] = None
    razonamiento: Optional[str] = None
    decision: Optional[str] = None
    decided_by: Optional[str] = None
    # Sprint 8: wa_user_onboarded
    phone: Optional[str] = None
    name: Optional[str] = None
    kind: Optional[str] = None
    source: Optional[str] = None
    contact_id: Optional[int] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None


# ---- Empresa contactos (destinatarios WhatsApp por empresa transportista) ----
class EmpresaSummary(BaseModel):
    """Resumen por empresa: cantidad de contactos, opt-in y última alerta."""
    empresa_id: int
    nombre: str
    activo: bool
    central_phone: Optional[str] = None
    contactos_count: int
    opted_in_count: int
    last_alert_at: Optional[str] = None


class ContactoOut(BaseModel):
    contact_id: int
    empresa_id: int
    nombre: str
    rol: str  # jefe / coordinador / dispatcher / driver / otro
    phone_e164: str
    email: Optional[str] = None
    severities_in: Optional[list[str]] = None  # NULL/None = todas
    motivos_in: Optional[list[str]] = None     # NULL/None = todos
    region_filter: str = "all"                  # RM | regiones | all
    opted_in_at: Optional[str] = None
    active: bool = True
    notes: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ContactoCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    rol: str  # validado contra ALLOWED_ROLES en empresa_contactos.py
    phone_e164: str = Field(min_length=9, max_length=20)
    email: Optional[str] = Field(default=None, max_length=200)
    severities_in: Optional[list[str]] = None
    motivos_in: Optional[list[str]] = None
    region_filter: Optional[str] = "all"
    notes: Optional[str] = Field(default=None, max_length=500)


class ContactoUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=200)
    rol: Optional[str] = None
    phone_e164: Optional[str] = Field(default=None, min_length=9, max_length=20)
    email: Optional[str] = Field(default=None, max_length=200)
    severities_in: Optional[list[str]] = None
    motivos_in: Optional[list[str]] = None
    region_filter: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=500)
    active: Optional[bool] = None


class BulkCSVResult(BaseModel):
    added: int
    skipped: list[dict]   # [{"row": int, "reason": str}]
    errors: list[dict]    # [{"row": int, "reason": str}]


class TestBroadcastRow(BaseModel):
    contact_id: int
    nombre: str
    phone: str
    status: str  # sent | dry_run | error | disabled
    twilio_sid: Optional[str] = None
    error: Optional[str] = None


class TestBroadcastResult(BaseModel):
    empresa_id: int
    body: str
    sent: int
    failed: int
    results: list[TestBroadcastRow]

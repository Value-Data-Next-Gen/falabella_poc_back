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


# Fase 2 MVP refactor: removidos KPIs, Visit, ShapFactor, AnticipatedAlert,
# VisitExplanation, CalibrationPoint, ModelMetrics, FeatureImportance,
# VehicleSummary, ClientMaster, IncidentRequest, ClockRequest — todos
# acoplados al modelo XGBoost + SHAP + STATE.snapshot_df ya eliminados.


# ---- Maestros ----
class Driver(BaseModel):
    driver_id: str
    name: str
    phone: Optional[str] = None
    license: Optional[str] = None
    empresa_id: Optional[int] = None
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    rating: Optional[float] = None
    deliveries_30d: Optional[int] = None
    fail_rate_30d: Optional[float] = None
    active: bool
    joined_at: Optional[str] = None


class VehicleExtended(BaseModel):
    vehicle_id: int
    empresa_id: Optional[int] = None
    name: Optional[str] = None
    type: Optional[str] = None
    plate: Optional[str] = None
    capacity_m3: Optional[int] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    depot_lat: Optional[float] = None
    depot_lon: Optional[float] = None
    active: bool
    year: Optional[int] = None


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
    # CR-014: activation token (wa.me link workaround para error 63112 de Meta).
    activation_token: Optional[str] = None
    activation_link: Optional[str] = None
    activation_used_at: Optional[str] = None


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

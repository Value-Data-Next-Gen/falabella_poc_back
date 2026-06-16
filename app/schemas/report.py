"""Schemas for operational reports (CR: day report + region/driver breakdown
+ day-over-day comparison)."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class OutcomeCounts(BaseModel):
    visitas: int
    entregado: int
    no_entregado: int
    cancelado: int
    pendiente: int
    success_pct: float | None  # entregado / terminal; None if no terminal visitas


class OnTime(BaseModel):
    """Punctuality over visitas that have both eta_estimada and completada_at."""
    medidas: int           # how many visitas could be measured
    a_tiempo: int          # completada_at <= eta_estimada + grace
    atrasadas: int
    on_time_pct: float | None
    avg_delay_min: float | None  # signed; negative = early on average
    grace_min: int


class RegionRow(BaseModel):
    region: str | None
    visitas: int
    entregado: int
    no_entregado: int
    success_pct: float | None


class DriverRow(BaseModel):
    driver_id: str | None
    nombre: str | None
    visitas: int
    entregado: int
    no_entregado: int
    cancelado: int
    success_pct: float | None
    on_time_pct: float | None
    avg_delay_min: float | None


class MotivoRow(BaseModel):
    motivo: str | None
    count: int


class Comparison(BaseModel):
    prev_dia_id: int | None
    prev_fecha: date | None
    visitas_delta: int | None
    success_pct_delta: float | None
    on_time_pct_delta: float | None


class DiaReport(BaseModel):
    dia_id: int
    fecha: date
    empresa_id: int
    empresa_nombre: str | None
    estado: str
    totals: OutcomeCounts
    vip: OutcomeCounts
    on_time: OnTime
    by_region: list[RegionRow]
    by_driver: list[DriverRow]
    by_motivo: list[MotivoRow]
    comparison: Comparison

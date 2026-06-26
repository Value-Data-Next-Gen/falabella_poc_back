"""Pydantic schemas for Capacitacion (training)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class CapacitacionCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    institucion: str | None = Field(default=None, max_length=200)
    fecha_realizacion: date | None = None
    fecha_vencimiento: date | None = None
    horas: int | None = Field(default=None, ge=1)
    notes: str | None = Field(default=None, max_length=500)


class CapacitacionUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=200)
    institucion: str | None = Field(default=None, max_length=200)
    fecha_realizacion: date | None = None
    fecha_vencimiento: date | None = None
    horas: int | None = Field(default=None, ge=1)
    estado: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=500)
    activo: bool | None = None


class CapacitacionOut(BaseModel):
    capacitacion_id: int
    driver_id: str
    nombre: str
    institucion: str | None
    fecha_realizacion: date | None
    fecha_vencimiento: date | None
    horas: int | None
    estado: str
    notes: str | None
    activo: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

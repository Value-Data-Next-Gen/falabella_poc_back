"""Pydantic schemas for Vehicle entity."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VehicleBase(BaseModel):
    empresa_id: int = Field(ge=1)
    nombre: str = Field(min_length=1, max_length=100)
    plate: str | None = Field(default=None, max_length=20)
    tipo: str | None = Field(default=None, max_length=50)
    capacity_m3: int | None = Field(default=None, ge=0, le=10000)
    descripcion: str | None = Field(default=None, max_length=500)
    year: int | None = Field(default=None, ge=1900, le=2100)
    depot_lat: float | None = Field(default=None, ge=-90, le=90)
    depot_lon: float | None = Field(default=None, ge=-180, le=180)


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    """All fields optional; cannot change empresa_id via PATCH (use admin DDL)."""

    nombre: str | None = Field(default=None, min_length=1, max_length=100)
    plate: str | None = Field(default=None, max_length=20)
    tipo: str | None = Field(default=None, max_length=50)
    capacity_m3: int | None = Field(default=None, ge=0, le=10000)
    descripcion: str | None = Field(default=None, max_length=500)
    year: int | None = Field(default=None, ge=1900, le=2100)
    depot_lat: float | None = Field(default=None, ge=-90, le=90)
    depot_lon: float | None = Field(default=None, ge=-180, le=180)
    activo: bool | None = None


class VehicleOut(VehicleBase):
    vehicle_id: int
    activo: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

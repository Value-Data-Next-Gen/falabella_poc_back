"""Pydantic schemas for Empresa entity."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EmpresaBase(BaseModel):
    nombre: str = Field(min_length=1, max_length=100)
    razon_social: str | None = Field(default=None, max_length=200)
    rut: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=100)
    comuna: str | None = Field(default=None, max_length=100)
    central_phone: str | None = Field(default=None, max_length=20)
    supervisor_phone_e164: str | None = Field(default=None, max_length=20)


class EmpresaCreate(EmpresaBase):
    pass


class EmpresaUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=100)
    razon_social: str | None = Field(default=None, max_length=200)
    rut: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=100)
    comuna: str | None = Field(default=None, max_length=100)
    central_phone: str | None = Field(default=None, max_length=20)
    supervisor_phone_e164: str | None = Field(default=None, max_length=20)
    activo: bool | None = None


class EmpresaOut(EmpresaBase):
    empresa_id: int
    activo: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EmpresaSummary(BaseModel):
    """Stats card for an empresa. Counts populated as entities ship in later CRs."""

    empresa_id: int
    nombre: str
    drivers_total: int = 0
    drivers_opted_in: int = 0
    vehicles_total: int = 0
    contactos_total: int = 0
    contactos_opted_in: int = 0

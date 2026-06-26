"""Pydantic schemas for Motivo."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MotivoCreate(BaseModel):
    codigo: str = Field(min_length=1, max_length=100)
    descripcion: str = Field(min_length=1, max_length=500)
    desambiguacion: str | None = Field(default=None, max_length=1000)
    severity: str = Field(default="low", pattern="^(low|medium|high|critical)$")
    alertable: bool = False
    orden: int = 0


class MotivoUpdate(BaseModel):
    codigo: str | None = Field(default=None, max_length=100)
    descripcion: str | None = Field(default=None, max_length=500)
    desambiguacion: str | None = Field(default=None, max_length=1000)
    severity: str | None = Field(default=None, pattern="^(low|medium|high|critical)$")
    alertable: bool | None = None
    activo: bool | None = None
    orden: int | None = None


class MotivoOut(BaseModel):
    motivo_id: int
    codigo: str
    descripcion: str
    desambiguacion: str | None
    severity: str
    alertable: bool
    activo: bool
    orden: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

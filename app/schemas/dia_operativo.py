"""Pydantic schemas for day lifecycle."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class DiaCreate(BaseModel):
    empresa_id: int
    fecha: date
    notas: str | None = None


class DiaOut(BaseModel):
    dia_id: int
    empresa_id: int
    fecha: date
    estado: str
    notas: str | None
    created_by_user_id: int | None
    validado_at: datetime | None
    iniciado_at: datetime | None
    cerrado_at: datetime | None
    created_at: datetime
    # Counts (populated by endpoint)
    rutas_count: int = 0
    visitas_count: int = 0
    visitas_entregadas: int = 0
    visitas_no_entregadas: int = 0

    model_config = ConfigDict(from_attributes=True)


class RutaCreate(BaseModel):
    driver_id: str
    vehicle_id: int | None = None
    notas: str | None = None


class RutaOut(BaseModel):
    ruta_id: int
    dia_id: int
    driver_id: str
    vehicle_id: int | None
    orden: int
    notas: str | None
    created_at: datetime
    # CR-019: Falabella-source folio/subfolio.
    folio: str | None = None
    subfolio: str | None = None
    # Populated
    driver_nombre: str = ""
    vehicle_patente: str = ""
    visitas_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class VisitaCreate(BaseModel):
    cliente_nombre: str = Field(min_length=1, max_length=200)
    cliente_rut: str | None = Field(default=None, max_length=20)
    cliente_telefono: str | None = Field(default=None, max_length=20)
    direccion: str = Field(min_length=1, max_length=500)
    comuna: str | None = Field(default=None, max_length=100)
    lat: float | None = None
    lon: float | None = None
    n_bultos: int = 1
    referencia: str | None = Field(default=None, max_length=100)
    es_vip: bool = False
    notas: str | None = Field(default=None, max_length=500)
    ruta_id: int | None = None


class VisitaUpdate(BaseModel):
    estado: str | None = None
    motivo: str | None = None
    motivo_comentario: str | None = None
    ruta_id: int | None = None
    orden: int | None = None
    notas: str | None = None


class VisitaOut(BaseModel):
    visita_id: int
    ruta_id: int | None
    dia_id: int
    empresa_id: int
    orden: int
    cliente_nombre: str
    cliente_rut: str | None
    cliente_telefono: str | None
    direccion: str
    comuna: str | None
    lat: float | None
    lon: float | None
    estado: str
    motivo: str | None
    motivo_comentario: str | None
    motivo_ia_sugerido: str | None
    eta_estimada: datetime | None
    llegada_at: datetime | None
    completada_at: datetime | None
    n_bultos: int
    referencia: str | None
    es_vip: bool | None
    notas: str | None
    created_at: datetime
    # CR-019: link to clientes master + Falabella-source fields.
    cliente_id: int | None = None
    folio_cliente: str | None = None
    subfolio_bulto: str | None = None
    parent_order: str | None = None
    tipo_documento: str | None = None
    region: str | None = None
    fecha_pactada: date | None = None
    estado_fuente: str | None = None

    model_config = ConfigDict(from_attributes=True)

"""Pydantic schemas for the Falabella XLSX/JSON ingest pipeline (CR-019)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class FalabellaRow(BaseModel):
    """One row of the Falabella source feed (1 row = 1 bulto)."""

    fechainicioruta: str | datetime
    patente_falsa: int = Field(ge=1, le=21)
    empresa_falsa: int = Field(description="22/23/25/27/33 mapped to v2 empresa_id 1..5")
    idruta: int
    Suborden: int | str | None = None
    do: int | str
    lpn: str | None = None
    parentorder: str | None = None
    direccion: str | None = None
    localidad: str | None = None
    region: str | None = None
    fechapactada: date | str | None = None
    tipodocumento: str | None = None
    estado: str | None = None
    motivonoentrega: str | None = None
    comentarionoentrega: str | None = None

    model_config = ConfigDict(extra="ignore")


class FalabellaJSONBody(BaseModel):
    rows: list[FalabellaRow]


class IngestResult(BaseModel):
    dia_ids: list[int]
    empresas_procesadas: int
    rutas_creadas: int
    visitas_creadas: int
    clientes_creados: int
    clientes_reusados: int
    # CR-027: `clientes_reusados_cross_empresa` was removed along with the
    # cliente_empresas table. Without that table we cannot accurately
    # distinguish "first-time link to this empresa" from "first-time cliente
    # ever" without re-querying visitas per row — the value was operationally
    # noisy anyway. The remaining `clientes_reusados` already captures
    # identity-level reuse by global RUT.
    vehiculos_creados: int = 0
    drivers_creados: int = 0
    geocoding_en_progreso: bool = False
    advertencias: list[str] = Field(default_factory=list)

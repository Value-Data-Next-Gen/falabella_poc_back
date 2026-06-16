"""Pydantic schemas for Cliente entity.

CR-027 model: the cliente master is IDENTITY-ONLY. It carries no tenant
relationship — no `empresa_id`, no `empresas_servidas`, no `ClienteEmpresaOut`.
The link to a transportista is always derived from the live operational chain
``empresa <- dias_operativos <- rutas <- visitas -> cliente``.

The ``visitas_total`` field is preserved on ``ClienteOut`` and computed by the
router on demand (``COUNT(visitas WHERE cliente_id=X)`` — scoped per role).

Historical notes:
  * CR-019: cliente had a rigid ``empresa_id``.
  * CR-023: relaxed to optional + introduced ``cliente_empresas`` M2M and
    ``empresas_servidas[_detalle]`` on the response.
  * CR-027: dropped both. Identity-only master.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DIAS_VALIDOS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _parse_dias(value: str | list[str] | None) -> list[str] | None:
    """Coerce a stored `dias_no_disponible` value (string JSON or list) to a
    validated list of weekday codes. NULL/None passes through.
    """
    if value is None:
        return None
    if isinstance(value, list):
        items = value
    else:
        # Stored as JSON text in DB. Tolerate malformed → empty list (better
        # than crashing a list endpoint over a corrupted single row).
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        items = parsed if isinstance(parsed, list) else []
    items = [str(x).lower() for x in items]
    bad = [x for x in items if x not in _DIAS_VALIDOS]
    if bad:
        raise ValueError(
            f"dias_no_disponible contains invalid codes {bad}; allowed: {sorted(_DIAS_VALIDOS)}"
        )
    # De-dupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

# ── Base ────────────────────────────────────────────────────────────────────


class ClienteBase(BaseModel):
    # CR-027: cliente is identity-only. No `empresa_id`.
    nombre: str = Field(min_length=1, max_length=200)
    rut: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    es_vip: bool = False
    vip_razon: str | None = Field(default=None, max_length=500)
    notas_operativas: str | None = Field(default=None, max_length=1000)
    direccion_default: str | None = Field(default=None, max_length=300)
    comuna_default: str | None = Field(default=None, max_length=100)
    region_default: str | None = Field(default=None, max_length=100)
    lat_default: float | None = Field(default=None, ge=-90, le=90)
    lon_default: float | None = Field(default=None, ge=-180, le=180)

    # CR-024 — operational rules. All opt-in (NULL by default).
    ventana_horaria_inicio: time | None = None
    ventana_horaria_fin: time | None = None
    dias_no_disponible: list[str] | None = Field(
        default=None,
        description="ISO weekday codes the cliente cannot receive deliveries (mon..sun).",
    )
    prioridad: int | None = Field(
        default=None, ge=1, le=5,
        description="1=highest, 5=lowest. NULL = unset.",
    )

    @field_validator("dias_no_disponible", mode="before")
    @classmethod
    def _v_dias(cls, v):  # noqa: D401, ANN001
        return _parse_dias(v)


class ClienteCreate(ClienteBase):
    pass


class ClienteUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=200)
    rut: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    es_vip: bool | None = None
    vip_razon: str | None = Field(default=None, max_length=500)
    notas_operativas: str | None = Field(default=None, max_length=1000)
    direccion_default: str | None = Field(default=None, max_length=300)
    comuna_default: str | None = Field(default=None, max_length=100)
    region_default: str | None = Field(default=None, max_length=100)
    lat_default: float | None = Field(default=None, ge=-90, le=90)
    lon_default: float | None = Field(default=None, ge=-180, le=180)

    # CR-024 — operational rules patchable like any other cliente field.
    ventana_horaria_inicio: time | None = None
    ventana_horaria_fin: time | None = None
    dias_no_disponible: list[str] | None = None
    prioridad: int | None = Field(default=None, ge=1, le=5)

    @field_validator("dias_no_disponible", mode="before")
    @classmethod
    def _v_dias(cls, v):  # noqa: D401, ANN001
        return _parse_dias(v)


# ── Out shapes ──────────────────────────────────────────────────────────────


class ClienteOut(ClienteBase):
    cliente_id: int
    created_at: datetime
    updated_at: datetime

    # CR-020: geocoding lifecycle exposed read-only (PATCH does not touch these).
    geocoding_status: str = "pending"
    geocoding_attempts: int = 0
    geocoded_at: datetime | None = None

    # CR-027: total visitas of this cliente as seen by the caller (scoped to
    # caller's empresa_ids when transport_manager; total across all empresas
    # when falabella_admin/ops). Computed live via COUNT(visitas).
    visitas_total: int = 0

    # CR-024: only present on PATCH responses when the change propagated to
    # active visitas (es_vip or notas_operativas modified). Absent otherwise so
    # GETs stay backward compatible.
    sync_visitas_count: int | None = None

    model_config = ConfigDict(from_attributes=True)


class ClienteListResponse(BaseModel):
    """CR-023: paginated wrapper for list endpoints.

    Response shape: ``{items, total, limit, offset}``.
    """

    items: list[ClienteOut]
    total: int
    limit: int
    offset: int


# ── Visita history ──────────────────────────────────────────────────────────


class ClienteVisitaHistorialItem(BaseModel):
    visita_id: int
    dia_id: int
    fecha: date | None
    ruta_id: int | None
    ruta_folio: str | None
    empresa_id: int
    empresa_nombre: str | None = None
    estado: str
    motivo: str | None
    eta_estimada: datetime | None
    direccion: str

    model_config = ConfigDict(from_attributes=True)


class ClienteVisitaHistorialResponse(BaseModel):
    items: list[ClienteVisitaHistorialItem]
    total: int
    limit: int
    offset: int


# ── CR-027: derived empresas-servidas (computed live from visitas) ──────────


class EmpresaServidaOut(BaseModel):
    """One row of the derived "empresas servidas" projection for a cliente.

    Computed live via:
        SELECT d.empresa_id, e.nombre,
               COUNT(v.visita_id), MIN(v.created_at), MAX(v.created_at)
        FROM visitas v
        JOIN dias_operativos d ON d.dia_id = v.dia_id
        JOIN empresas e ON e.empresa_id = d.empresa_id
        WHERE v.cliente_id = :id
        GROUP BY d.empresa_id, e.nombre

    Scope-filtered to caller's `empresa_ids` when transport_manager.
    """

    empresa_id: int
    empresa_nombre: str | None = None
    visitas_count: int
    first_at: datetime | None = None
    last_at: datetime | None = None


# ── CR-024 — Cancel pending visitas request/response ────────────────────────


class CancelPendingVisitasRequest(BaseModel):
    """Body for POST /api/v1/clientes/{id}/cancel-pending-visitas.

    Scopes:
      * `all` — cancel every pendiente/en_camino across BORRADOR/VALIDADO/EN_CURSO
        dias for this cliente.
      * `today` — only dias whose `fecha == today`.
      * `next_n_days` — dias whose `fecha` is in [today, today+dias]. `dias`
        is required and bounded 1..30.
    """

    motivo: str = Field(min_length=1, max_length=500)
    scope: Literal["all", "today", "next_n_days"] = "all"
    dias: int | None = Field(default=None, ge=1, le=30)


class CancelPendingVisitasResult(BaseModel):
    """Result of a bulk cancel. `dia_ids` / `visita_ids` let the caller
    invalidate caches on a per-day basis instead of re-fetching everything.
    """

    cancelled_count: int
    dia_ids: list[int]
    visita_ids: list[int]


# ── CR-024 — Visitas futuras (lookahead) ────────────────────────────────────


class ClienteVisitaProgramadaItem(BaseModel):
    """One row of the visitas-futuras endpoint. Mirrors historial but is
    pendiente/en_camino-only and exposes the day's state so the UI can show
    whether the visita is in a planning vs operational day.
    """

    visita_id: int
    dia_id: int
    fecha: date | None
    ruta_id: int | None
    ruta_folio: str | None
    empresa_id: int
    empresa_nombre: str | None = None
    estado: str
    eta_estimada: datetime | None
    direccion: str
    comuna: str | None = None
    dia_estado: str

    model_config = ConfigDict(from_attributes=True)


class ClienteVisitasFuturasResponse(BaseModel):
    items: list[ClienteVisitaProgramadaItem]
    total: int
    dias_lookahead: int

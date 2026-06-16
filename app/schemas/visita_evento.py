"""Pydantic schemas for the visita audit log + the request bodies that drive it.

CR-028 Part A. See `app.db.models.visita_evento` for the ORM mirror.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Request bodies for the new operacion endpoints
# ---------------------------------------------------------------------------


class VisitaOrdenIn(BaseModel):
    """PATCH /api/v1/operacion/visitas/{visita_id}/orden body."""

    nuevo_orden: int = Field(ge=1, description="New 1-based position within the route")


class VisitaCancelIn(BaseModel):
    """POST /api/v1/operacion/visitas/{visita_id}/cancel body.

    `motivo_codigo` must match a row in `td.motivos` (catalogo oficial). The
    handler validates this and returns 400 otherwise.
    """

    motivo_codigo: str = Field(min_length=1, max_length=100)
    comentario: str | None = Field(default=None, max_length=500)


class VisitaMoveRouteIn(BaseModel):
    """POST /api/v1/operacion/visitas/{visita_id}/move-route body.

    Origin and destination rutas must belong to the same `dia`. If `nuevo_orden`
    is omitted, the visita is appended at the end of the destination route.
    """

    nueva_ruta_id: int
    nuevo_orden: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class PromoteVipsResult(BaseModel):
    """POST /api/v1/operacion/rutas/{ruta_id}/promote-vips response."""

    ruta_id: int
    vips_promoted: int = Field(
        description="How many VIP visitas had their orden updated"
    )
    visitas_reordered: int = Field(
        description="Total pending visitas whose orden was touched (VIPs + tail)"
    )


class PlanEtasWarning(BaseModel):
    """One warning entry from POST /dias/{id}/plan-etas with reglas activas."""

    visita_id: int
    reason: str


class PlanEtasResult(BaseModel):
    """POST /api/v1/operacion/dias/{dia_id}/plan-etas response.

    Backwards compatible with the CR-019 shape (`visitas_planificadas`,
    `shift_start`, `duracion_horas`). When `respetar_reglas_cliente=true` we
    also return `warnings` describing visitas that were skipped because the
    cliente is unavailable on the day's weekday.
    """

    dia_id: int
    visitas_planificadas: int
    shift_start: str
    duracion_horas: int
    respetar_reglas_cliente: bool = False
    warnings: list[PlanEtasWarning] = Field(default_factory=list)


class VisitaEventoOut(BaseModel):
    """One row of `td.visita_eventos`.

    `payload` is the parsed JSON from `payload_json`. We expose it as a dict
    (or None) so the frontend doesn't have to re-parse.
    """

    evento_id: int
    visita_id: int
    tipo: str
    user_id: int | None
    payload: dict[str, Any] | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("payload", mode="before")
    @classmethod
    def _parse_payload(cls, v: Any) -> Any:
        """Accept either a pre-parsed dict (rare) or the raw JSON string from the DB."""
        if v is None or isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                # Defensive: never let a malformed audit row break the listing.
                return {"_raw": v}
        return v

    @classmethod
    def from_orm_row(cls, row: Any) -> VisitaEventoOut:
        """Build from a SQLAlchemy `VisitaEvento` row, parsing payload_json."""
        return cls(
            evento_id=row.evento_id,
            visita_id=row.visita_id,
            tipo=row.tipo,
            user_id=row.user_id,
            payload=row.payload_json,  # validator parses it
            created_at=row.created_at,
        )

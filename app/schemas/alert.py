"""Pydantic schemas for alerts (CR-022 Part A).

Wire contract for `/api/v1/alerts/*`. The ORM model lives in
`app.db.models.alert.Alert`. The dispatcher returns `AlertDispatchResult` so
the cron / manual endpoint can log how many recipients got notified.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AlertTipo = Literal["eta_breach", "eta_preview", "vip_deadline", "manual"]
AlertSeverity = Literal["baja", "media", "alta", "critica"]
AlertEstado = Literal["abierta", "notificada", "resuelta", "descartada"]


class AlertOut(BaseModel):
    alert_id: int
    tipo: AlertTipo
    severity: AlertSeverity
    empresa_id: int
    dia_id: int | None
    ruta_id: int | None
    visita_id: int | None
    descripcion: str
    payload_json: str | None
    estado: AlertEstado
    created_at: datetime
    notified_at: datetime | None
    notified_recipients_count: int
    resolved_at: datetime | None
    resolved_by_user_id: int | None
    owner_user_id: int | None = None
    acked_at: datetime | None = None
    dedupe_key: str | None

    model_config = ConfigDict(from_attributes=True)


class AlertCreate(BaseModel):
    """Body for POST /api/v1/alerts/manual.

    `tipo` is hardcoded to `'manual'` server-side; we keep it out of the body
    so a transport_manager can't sneak in a fake `eta_breach` and bypass
    cron-side dedupe logic.
    """

    empresa_id: int
    severity: AlertSeverity = "media"
    dia_id: int | None = None
    ruta_id: int | None = None
    visita_id: int | None = None
    descripcion: str = Field(min_length=1, max_length=500)
    payload_json: str | None = Field(default=None, max_length=4000)
    auto_dispatch: bool = False


class AlertUpdate(BaseModel):
    """Body for PATCH /api/v1/alerts/{alert_id}.

    Only `estado` transitions are allowed via PATCH. `resolved_at` and
    `resolved_by_user_id` are derived server-side from current_user + now.
    """

    estado: Literal["resuelta", "descartada"]


class AlertDispatchResult(BaseModel):
    alert_id: int
    recipients: int = Field(
        description="Total recipients matched after severity/motivo filtering."
    )
    sent: int = Field(description="Recipients to whom send_whatsapp returned True.")
    dry_run: bool

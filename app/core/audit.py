"""Audit log helpers.

CR-028 Part A. Centralizes how operacion endpoints record visita-level events
into `td.visita_eventos`. Keep callers terse:

    await log_visita_evento(
        db,
        visita_id=v.visita_id,
        tipo="orden_change",
        user_id=user.user_id,
        payload={"old_orden": old, "nuevo_orden": new},
    )

The helper does NOT commit — caller decides the transaction boundary. This is
deliberate: every endpoint that audits a change also mutates the row(s) being
audited, and we want a single commit covering both.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.visita_evento import TIPOS, VisitaEvento

_PAYLOAD_MAX_BYTES = 2000


async def log_visita_evento(
    db: AsyncSession,
    *,
    visita_id: int,
    tipo: str,
    user_id: int | None,
    payload: dict[str, Any] | None = None,
) -> VisitaEvento:
    """Append a row to `td.visita_eventos`. Validates `tipo` and serializes payload.

    Raises:
        ValueError: if `tipo` is not in the allowed enumeration (mirrors the DB
            CHECK constraint so we fail fast in app code instead of bouncing
            off SQL Server).
    """
    if tipo not in TIPOS:
        raise ValueError(f"Unknown visita_eventos.tipo: {tipo!r}. Allowed: {TIPOS}")

    payload_str: str | None = None
    if payload is not None:
        payload_str = json.dumps(payload, ensure_ascii=False, default=str)
        if len(payload_str.encode("utf-8")) > _PAYLOAD_MAX_BYTES:
            # Truncate defensively. We never want an audit row to crash the
            # business write — but we mark it so consumers can detect it.
            marker = '..."_trunc":true}'
            payload_str = payload_str[: _PAYLOAD_MAX_BYTES - len(marker)] + marker

    evento = VisitaEvento(
        visita_id=visita_id,
        tipo=tipo,
        user_id=user_id,
        payload_json=payload_str,
    )
    db.add(evento)
    return evento

"""CR-013 — Copiloto IA: persistencia de decisiones del operador.

POST /api/copiloto/decisions
    Registra la decisión (intent) del operador sobre una sugerencia del copiloto.
    Sirve para fine-tuning del modelo + métricas de utilidad + auditoría.

El listado/feed de sugerencias (`GET /api/copiloto/suggestions`) NO está acá:
sigue siendo mock client-side (ver `OperacionModuleV2.MOCK_SUGGESTIONS` en
frontend) hasta que se conecte al LLM real (ROADMAP).

Diseño:
- `fecha` se deriva server-side de STATE.today (día operativo activo) con
  fallback a UTC today si STATE no está booteado todavía. NO se acepta del
  cliente — si el operador ve un día desactualizado, igual la decisión queda
  vinculada al día op real en DB.
- `empresa_id` se toma del JWT (CurrentUser). Falabella ops/admin tienen
  empresa_id=NULL → la decisión queda persistida sin scope de empresa
  (intencional: las sugerencias de copiloto pueden ser cross-empresa).
- `user_email` se persiste como string libre (no FK). Aunque el usuario sea
  eliminado, la trazabilidad de la decisión se mantiene.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import get_conn
from core.state import STATE


router = APIRouter(prefix="/api/copiloto", tags=["copiloto"])


# Mantener sincronizado con el CHECK constraint de la migración 024.
CopilotoIntent = Literal[
    "escalate_supervisor",
    "retry_driver_alert",
    "review_visits",
    "mark_incident",
    "ignore",
]


# ---------- Schemas ----------
class CopilotoDecisionIn(BaseModel):
    suggestion_id: str = Field(..., max_length=128, min_length=1)
    intent: CopilotoIntent
    tracking_id: Optional[str] = Field(default=None, max_length=64)
    payload: Optional[dict[str, Any]] = None


class CopilotoDecisionOut(BaseModel):
    decision_id: int
    created_at: datetime


# ---------- Helpers ----------
def _today_op() -> date:
    """Día operativo activo. Si STATE no está booteado todavía cae a UTC today."""
    try:
        if STATE.today is not None:
            return STATE.today
    except Exception:  # noqa: BLE001
        pass
    return datetime.now(timezone.utc).date()


# ---------- Endpoints ----------
@router.post("/decisions", response_model=CopilotoDecisionOut)
def log_decision(
    req: CopilotoDecisionIn,
    user: CurrentUser = Depends(current_user),
) -> CopilotoDecisionOut:
    fecha_iso = _today_op().isoformat()
    payload_json = json.dumps(req.payload, ensure_ascii=False) if req.payload else None

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            INSERT INTO fpoc.copiloto_decisions
              (user_email, empresa_id, fecha, suggestion_id, intent,
               tracking_id, payload_json)
            OUTPUT INSERTED.decision_id, INSERTED.created_at
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            user.email, user.empresa_id, fecha_iso,
            req.suggestion_id, req.intent, req.tracking_id, payload_json,
        )
        row = cur.fetchone()
        if row is None:
            cn.rollback()
            raise HTTPException(500, "INSERT no devolvió fila")
        decision_id = int(row[0])
        created_at_raw = row[1]
        cn.commit()

    # Normalize created_at to a datetime (pyodbc devuelve datetime; sqlite, str)
    if isinstance(created_at_raw, datetime):
        created_at = created_at_raw
    elif isinstance(created_at_raw, str):
        # SQLite CURRENT_TIMESTAMP → 'YYYY-MM-DD HH:MM:SS'
        try:
            created_at = datetime.fromisoformat(created_at_raw.replace(" ", "T"))
        except ValueError:
            created_at = datetime.now(timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    logger.info(
        f"[copiloto] decision id={decision_id} user={user.email} "
        f"intent={req.intent} suggestion={req.suggestion_id} "
        f"tracking={req.tracking_id or '-'}"
    )
    return CopilotoDecisionOut(decision_id=decision_id, created_at=created_at)

"""Alerts CRUD + manual create + manual dispatch (CR-022 Part A).

Multi-tenancy mirrors `/api/v1/operacion`: list is filtered via `apply_scope`
on `Alert.empresa_id`; per-id GET/PATCH/dispatch loads the row and runs
`_check_alert_scope(user, alert)` before any mutation. Cross-tenant probes
return 403 (not 404) so an attacker can't enumerate IDs.

Endpoints:
  GET    /api/v1/alerts                  — list, scoped, with filters
  GET    /api/v1/alerts/{id}             — one row, scoped
  POST   /api/v1/alerts/manual           — operator-created alert (tipo='manual')
  PATCH  /api/v1/alerts/{id}             — transition estado to resuelta/descartada
  POST   /api/v1/alerts/{id}/dispatch    — re-dispatch (admin only)
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_dispatcher import dispatch_alert
from app.core.security import current_user
from app.core.security.scope import apply_scope, can_access_empresa
from app.db.models.alert import Alert
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.alert import (
    AlertCreate,
    AlertDispatchResult,
    AlertOut,
    AlertUpdate,
)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _check_alert_scope(user: User, alert: Alert) -> None:
    """Raise 403 if `user` cannot access `alert.empresa_id`."""
    if not can_access_empresa(user, alert.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")


async def _load_alert_for_user(db: AsyncSession, alert_id: int, user: User) -> Alert:
    alert = (await db.execute(select(Alert).where(Alert.alert_id == alert_id))).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alerta no encontrada")
    _check_alert_scope(user, alert)
    return alert


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------

@router.get("", operation_id="listAlerts", response_model=list[AlertOut])
async def list_alerts(
    empresa_id: int | None = None,
    dia_id: int | None = None,
    estado: str | None = None,
    severity: str | None = None,
    tipo: str | None = None,
    since: datetime | None = None,
    incluir_cerradas: bool = Query(
        default=False,
        description=(
            "If False (default), excludes alerts whose parent dia is CERRADO. "
            "Manual alerts without dia_id are always included. Set True to see "
            "the full history regardless of dia state."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AlertOut]:
    """List alerts, scoped to the caller's empresas.

    By default hides alerts tied to a closed dia (CR-025) — when a dia
    transitions to CERRADO every open alert on it is auto-resolved, but the
    rows stay around for audit. Pass `incluir_cerradas=true` to include them
    anyway. Alerts without `dia_id` (manual flags) are always shown because
    they are not tied to the day lifecycle.
    """
    stmt = select(Alert).order_by(Alert.created_at.desc())
    stmt = apply_scope(stmt, user, Alert.empresa_id)
    if not incluir_cerradas:
        # LEFT OUTER JOIN so alerts with NULL dia_id survive the filter.
        stmt = stmt.outerjoin(
            DiaOperativo, Alert.dia_id == DiaOperativo.dia_id
        ).where(
            or_(
                Alert.dia_id.is_(None),
                DiaOperativo.estado != "CERRADO",
            )
        )
    if empresa_id is not None:
        stmt = stmt.where(Alert.empresa_id == empresa_id)
    if dia_id is not None:
        stmt = stmt.where(Alert.dia_id == dia_id)
    if estado is not None:
        stmt = stmt.where(Alert.estado == estado)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    if tipo is not None:
        stmt = stmt.where(Alert.tipo == tipo)
    if since is not None:
        stmt = stmt.where(Alert.created_at >= since)
    stmt = stmt.offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [AlertOut.model_validate(r) for r in rows]


@router.get("/{alert_id}", operation_id="getAlert", response_model=AlertOut)
async def get_alert(
    alert_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertOut:
    alert = await _load_alert_for_user(db, alert_id, user)
    return AlertOut.model_validate(alert)


@router.post(
    "/manual",
    operation_id="createManualAlert",
    response_model=AlertOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_alert(
    body: AlertCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertOut:
    """Create an operator-flagged alert. `tipo` is forced to 'manual'.

    Roles allowed: admin / ops / transport_manager (with scope on
    `body.empresa_id`). If `body.auto_dispatch` is True, fan out via
    `dispatch_alert` and reflect the resulting state in the response.
    """
    if user.role not in ("falabella_admin", "falabella_ops", "transport_manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Rol no autorizado")
    if not can_access_empresa(user, body.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    alert = Alert(
        tipo="manual",
        severity=body.severity,
        empresa_id=body.empresa_id,
        dia_id=body.dia_id,
        ruta_id=body.ruta_id,
        visita_id=body.visita_id,
        descripcion=body.descripcion,
        payload_json=body.payload_json,
        estado="abierta",
        dedupe_key=None,  # manuals don't dedupe — operator owns the call.
    )
    db.add(alert)
    await db.commit()
    await db.refresh(alert)

    if body.auto_dispatch:
        await dispatch_alert(db, alert)
        await db.refresh(alert)

    return AlertOut.model_validate(alert)


@router.patch("/{alert_id}", operation_id="updateAlert", response_model=AlertOut)
async def update_alert(
    alert_id: int,
    body: AlertUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertOut:
    """Transition estado to `resuelta` or `descartada`. Sets resolved_* fields."""
    alert = await _load_alert_for_user(db, alert_id, user)
    if alert.estado in ("resuelta", "descartada"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"La alerta ya está en estado {alert.estado!r}",
        )
    alert.estado = body.estado
    alert.resolved_at = datetime.now(UTC)
    alert.resolved_by_user_id = user.user_id
    await db.commit()
    await db.refresh(alert)
    return AlertOut.model_validate(alert)


@router.post(
    "/{alert_id}/dispatch",
    operation_id="dispatchAlert",
    response_model=AlertDispatchResult,
)
async def dispatch_existing_alert(
    alert_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertDispatchResult:
    """Re-fan-out an alert. Admin only.

    If the alert is `notificada` we revert it to `abierta` so the dispatcher's
    idempotency guard doesn't skip it. resuelta/descartada are rejected.
    """
    if user.role != "falabella_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo admin puede re-disparar")
    alert = await _load_alert_for_user(db, alert_id, user)
    if alert.estado in ("resuelta", "descartada"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No se puede re-disparar una alerta {alert.estado!r}",
        )
    if alert.estado == "notificada":
        alert.estado = "abierta"
        await db.commit()
        await db.refresh(alert)
    return await dispatch_alert(db, alert)


@router.post("/{alert_id}/ack", operation_id="ackAlert", response_model=AlertOut)
async def ack_alert(
    alert_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertOut:
    """Claim an alert ('Yo me encargo') so other operators see it's owned.
    Passing it again by another user re-assigns ownership."""
    from datetime import UTC, datetime
    alert = await _load_alert_for_user(db, alert_id, user)
    if alert.estado in ("resuelta", "descartada"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Alerta ya {alert.estado!r}")
    alert.owner_user_id = user.user_id
    alert.acked_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(alert)
    return AlertOut.model_validate(alert)


@router.post("/{alert_id}/release", operation_id="releaseAlert", response_model=AlertOut)
async def release_alert(
    alert_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertOut:
    """Release a claimed alert (un-assign ownership)."""
    alert = await _load_alert_for_user(db, alert_id, user)
    alert.owner_user_id = None
    alert.acked_at = None
    await db.commit()
    await db.refresh(alert)
    return AlertOut.model_validate(alert)

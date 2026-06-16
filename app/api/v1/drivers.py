"""Drivers endpoints (scoped CRUD)."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user
from app.core.security.scope import apply_scope, can_access_empresa
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.session import get_db
from app.schemas.driver import (
    DriverCreate,
    DriverOut,
    DriverUpdate,
    RegenerateActivationResponse,
)

router = APIRouter(prefix="/api/v1/drivers", tags=["drivers"])


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _gen_activation_token() -> str:
    """16 random bytes URL-safe → ~22 chars. ≥128 bits entropy."""
    return secrets.token_urlsafe(16)


def _build_activation_link(token: str) -> str:
    """`https://wa.me/<sender>?text=ACTIVAR%20<token>`."""
    sender = settings.twilio_whatsapp_from.replace("whatsapp:", "").lstrip("+")
    return f"https://wa.me/{sender}?text=ACTIVAR%20{token}"


async def _next_driver_id(db: AsyncSession, empresa_id: int) -> str:
    """Sequential ID `DRV-{empresa_id:02d}{seq:03d}` per empresa."""
    prefix = f"DRV-{empresa_id:02d}"
    result = await db.execute(
        select(func.count(Driver.driver_id)).where(Driver.driver_id.like(f"{prefix}%"))
    )
    n = int(result.scalar_one() or 0) + 1
    return f"{prefix}{n:03d}"


def _require_can_write(user: User) -> None:
    if user.role not in ("falabella_admin", "falabella_ops", "transport_manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires admin/ops/manager role")


async def _validate_unique_identity(
    db: AsyncSession, rut: str | None, phone: str | None, exclude_driver_id: str | None = None,
) -> None:
    """A person (by RUT or phone) can only be an active driver in one empresa at a time."""
    if rut:
        stmt = select(Driver).where(Driver.rut == rut, Driver.activo == True)  # noqa: E712
        if exclude_driver_id:
            stmt = stmt.where(Driver.driver_id != exclude_driver_id)
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"RUT {rut} ya registrado como conductor activo: {existing.driver_id} ({existing.nombre}) en empresa {existing.empresa_id}",
            )
    if phone:
        stmt = select(Driver).where(Driver.phone_e164 == phone, Driver.activo == True)  # noqa: E712
        if exclude_driver_id:
            stmt = stmt.where(Driver.driver_id != exclude_driver_id)
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Telefono {phone} ya registrado como conductor activo: {existing.driver_id} ({existing.nombre}) en empresa {existing.empresa_id}",
            )


async def _validate_vehicle(
    db: AsyncSession, vehicle_id: int | None, empresa_id: int, exclude_driver_id: str | None = None,
) -> None:
    if vehicle_id is None:
        return
    veh = (await db.execute(select(Vehicle).where(Vehicle.vehicle_id == vehicle_id))).scalar_one_or_none()
    if veh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehiculo no encontrado")
    if veh.empresa_id != empresa_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El vehiculo pertenece a otra empresa")
    stmt = select(Driver).where(Driver.vehicle_id == vehicle_id, Driver.activo == True)  # noqa: E712
    if exclude_driver_id:
        stmt = stmt.where(Driver.driver_id != exclude_driver_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Vehiculo {veh.plate} ya asignado al conductor {existing.driver_id} ({existing.nombre})",
        )


async def _get_or_404(db: AsyncSession, driver_id: str) -> Driver:
    result = await db.execute(select(Driver).where(Driver.driver_id == driver_id))
    d = result.scalar_one_or_none()
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Driver not found")
    return d


# ----------------------------------------------------------------------------
# List
# ----------------------------------------------------------------------------

@router.get(
    "",
    operation_id="listDrivers",
    response_model=list[DriverOut],
)
async def list_drivers(
    empresa_id: int | None = Query(default=None),
    vehicle_id: int | None = Query(default=None),
    q: str | None = Query(default=None, description="search nombre / driver_id / phone"),
    active: bool | None = Query(default=None),
    opted_in: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DriverOut]:
    stmt = select(Driver)
    stmt = apply_scope(stmt, user, Driver.empresa_id)
    if empresa_id is not None:
        stmt = stmt.where(Driver.empresa_id == empresa_id)
    if vehicle_id is not None:
        stmt = stmt.where(Driver.vehicle_id == vehicle_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Driver.nombre.ilike(like), Driver.driver_id.ilike(like), Driver.phone_e164.ilike(like))
        )
    if active is not None:
        stmt = stmt.where(Driver.activo.is_(active))
    if opted_in is True:
        stmt = stmt.where(Driver.opted_in_at.is_not(None))
    elif opted_in is False:
        stmt = stmt.where(Driver.opted_in_at.is_(None))
    stmt = stmt.order_by(Driver.driver_id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return [DriverOut.model_validate(d) for d in result.scalars().all()]


# ----------------------------------------------------------------------------
# Get one
# ----------------------------------------------------------------------------

@router.get(
    "/{driver_id}",
    operation_id="getDriver",
    response_model=DriverOut,
)
async def get_driver(
    driver_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> DriverOut:
    d = await _get_or_404(db, driver_id)
    if not can_access_empresa(user, d.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    return DriverOut.model_validate(d)


# ----------------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------------

@router.post(
    "",
    operation_id="createDriver",
    response_model=DriverOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "Out of scope or insufficient role"},
        404: {"description": "Empresa or Vehicle not found"},
        409: {"description": "driver_id or phone_e164 already exists"},
    },
)
async def create_driver(
    body: DriverCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> DriverOut:
    _require_can_write(user)
    if not can_access_empresa(user, body.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot create driver in another empresa")

    # Verify empresa
    if (await db.execute(select(Empresa).where(Empresa.empresa_id == body.empresa_id))).scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")
    await _validate_unique_identity(db, body.rut, body.phone_e164)
    await _validate_vehicle(db, body.vehicle_id, body.empresa_id)

    driver_id = body.driver_id or await _next_driver_id(db, body.empresa_id)
    token = _gen_activation_token()

    driver = Driver(
        driver_id=driver_id,
        empresa_id=body.empresa_id,
        vehicle_id=body.vehicle_id,
        nombre=body.nombre,
        rut=body.rut,
        phone_e164=body.phone_e164,
        notify_whatsapp=body.notify_whatsapp,
        activation_token=token,
    )
    db.add(driver)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        msg = str(e).lower()
        if "driver_id" in msg or "primary key" in msg:
            raise HTTPException(status.HTTP_409_CONFLICT, f"driver_id already exists: {driver_id}") from None
        if "phone" in msg or "unique" in msg:
            raise HTTPException(status.HTTP_409_CONFLICT, f"phone already exists: {body.phone_e164}") from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"DB constraint: {e}") from None
    await db.refresh(driver)
    return DriverOut.model_validate(driver)


# ----------------------------------------------------------------------------
# Update
# ----------------------------------------------------------------------------

@router.patch(
    "/{driver_id}",
    operation_id="updateDriver",
    response_model=DriverOut,
)
async def update_driver(
    driver_id: str,
    body: DriverUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> DriverOut:
    _require_can_write(user)
    driver = await _get_or_404(db, driver_id)
    if not can_access_empresa(user, driver.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    if body.activo is not None and user.role == "transport_manager":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager cannot toggle 'activo'")

    if body.rut is not None or body.phone_e164 is not None:
        await _validate_unique_identity(db, body.rut, body.phone_e164, exclude_driver_id=driver.driver_id)
    if body.vehicle_id is not None:
        await _validate_vehicle(db, body.vehicle_id, driver.empresa_id, exclude_driver_id=driver.driver_id)

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(driver, key, value)
    driver.updated_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Conflict: {e}") from None
    await db.refresh(driver)
    return DriverOut.model_validate(driver)


# ----------------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------------

@router.delete(
    "/{driver_id}",
    operation_id="deactivateDriver",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deactivate_driver(
    driver_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user.role not in ("falabella_admin", "falabella_ops"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admin/ops can deactivate drivers")
    driver = await _get_or_404(db, driver_id)
    driver.activo = False
    driver.updated_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------
# Regenerate activation token
# ----------------------------------------------------------------------------

@router.post(
    "/{driver_id}/regenerate-activation",
    operation_id="regenerateDriverActivation",
    response_model=RegenerateActivationResponse,
    summary="Regenerate the activation token + wa.me link. Invalidates previous token.",
)
async def regenerate_activation(
    driver_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> RegenerateActivationResponse:
    _require_can_write(user)
    driver = await _get_or_404(db, driver_id)
    if not can_access_empresa(user, driver.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    new_token = _gen_activation_token()
    driver.activation_token = new_token
    driver.activation_used_at = None  # invalidate previous redemption (re-onboarding)
    driver.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(driver)

    return RegenerateActivationResponse(
        driver_id=driver.driver_id,
        activation_token=new_token,
        activation_link=_build_activation_link(new_token),
        expires_at=None,  # CR-009 will add INVITATION_TTL_DAYS
    )

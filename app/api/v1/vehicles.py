"""Vehicles endpoints (scoped CRUD).

  GET    /api/v1/vehicles            listVehicles      (scoped, filters)
  GET    /api/v1/vehicles/{id}       getVehicle        (scoped)
  POST   /api/v1/vehicles            createVehicle     (admin/manager)
  PATCH  /api/v1/vehicles/{id}       updateVehicle     (scoped)
  DELETE /api/v1/vehicles/{id}       deactivateVehicle (scoped, soft)
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.core.security.scope import apply_scope, can_access_empresa
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.session import get_db
from app.schemas.vehicle import VehicleCreate, VehicleOut, VehicleUpdate

router = APIRouter(prefix="/api/v1/vehicles", tags=["vehicles"])


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _require_can_write(user: User) -> None:
    """admin/ops/manager can write; driver/no-role cannot."""
    if user.role not in ("falabella_admin", "falabella_ops", "transport_manager"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Requires admin, ops, or manager role"
        )


async def _get_or_404(db: AsyncSession, vehicle_id: int) -> Vehicle:
    result = await db.execute(select(Vehicle).where(Vehicle.vehicle_id == vehicle_id))
    v = result.scalar_one_or_none()
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
    return v


# ----------------------------------------------------------------------------
# List
# ----------------------------------------------------------------------------

@router.get(
    "",
    operation_id="listVehicles",
    response_model=list[VehicleOut],
    summary="List vehicles (scoped).",
)
async def list_vehicles(
    empresa_id: int | None = Query(default=None, description="filter by empresa"),
    q: str | None = Query(default=None, description="search nombre / plate"),
    active: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VehicleOut]:
    stmt = select(Vehicle)
    stmt = apply_scope(stmt, user, Vehicle.empresa_id)
    if empresa_id is not None:
        # If manager filters by foreign empresa → empty (scope already filters).
        stmt = stmt.where(Vehicle.empresa_id == empresa_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Vehicle.nombre.ilike(like), Vehicle.plate.ilike(like)))
    if active is not None:
        stmt = stmt.where(Vehicle.activo.is_(active))
    stmt = stmt.order_by(Vehicle.vehicle_id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return [VehicleOut.model_validate(v) for v in result.scalars().all()]


# ----------------------------------------------------------------------------
# Get one
# ----------------------------------------------------------------------------

@router.get(
    "/{vehicle_id}",
    operation_id="getVehicle",
    response_model=VehicleOut,
    responses={
        403: {"description": "Out of scope"},
        404: {"description": "Vehicle not found"},
    },
)
async def get_vehicle(
    vehicle_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> VehicleOut:
    v = await _get_or_404(db, vehicle_id)
    if not can_access_empresa(user, v.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    return VehicleOut.model_validate(v)


# ----------------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------------

@router.post(
    "",
    operation_id="createVehicle",
    response_model=VehicleOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "Out of scope or insufficient role"},
        409: {"description": "Plate already exists"},
        404: {"description": "Empresa not found"},
    },
)
async def create_vehicle(
    body: VehicleCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> VehicleOut:
    _require_can_write(user)
    if not can_access_empresa(user, body.empresa_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Cannot create vehicle in another empresa",
        )
    # Verify empresa exists (CASCADE FK would fail at commit, but clearer 404 here).
    emp = await db.execute(select(Empresa).where(Empresa.empresa_id == body.empresa_id))
    if emp.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")

    vehicle = Vehicle(
        empresa_id=body.empresa_id,
        nombre=body.nombre,
        plate=body.plate,
        tipo=body.tipo,
        capacity_m3=body.capacity_m3,
        year=body.year,
        depot_lat=body.depot_lat,
        depot_lon=body.depot_lon,
    )
    db.add(vehicle)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        msg = str(e).lower()
        if "plate" in msg or "unique" in msg or "duplicate" in msg:
            raise HTTPException(status.HTTP_409_CONFLICT, f"plate already exists: {body.plate}") from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"DB constraint: {e}") from None
    await db.refresh(vehicle)
    return VehicleOut.model_validate(vehicle)


# ----------------------------------------------------------------------------
# Update
# ----------------------------------------------------------------------------

@router.patch(
    "/{vehicle_id}",
    operation_id="updateVehicle",
    response_model=VehicleOut,
)
async def update_vehicle(
    vehicle_id: int,
    body: VehicleUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> VehicleOut:
    _require_can_write(user)
    vehicle = await _get_or_404(db, vehicle_id)
    if not can_access_empresa(user, vehicle.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    if body.activo is not None and user.role == "transport_manager":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Manager cannot toggle 'activo'; admin/ops only",
        )

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(vehicle, key, value)
    vehicle.updated_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Conflict: {e}") from None
    await db.refresh(vehicle)
    return VehicleOut.model_validate(vehicle)


# ----------------------------------------------------------------------------
# Delete (soft)
# ----------------------------------------------------------------------------

@router.delete(
    "/{vehicle_id}",
    operation_id="deactivateVehicle",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete (sets activo=false). Admin / ops only.",
)
async def deactivate_vehicle(
    vehicle_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user.role not in ("falabella_admin", "falabella_ops"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only admin/ops can deactivate vehicles"
        )
    vehicle = await _get_or_404(db, vehicle_id)
    vehicle.activo = False
    vehicle.updated_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

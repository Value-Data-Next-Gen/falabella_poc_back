"""Empresas (tenants) endpoints.

  GET    /api/v1/empresas               list (scoped)
  GET    /api/v1/empresas/me            return current manager's empresa (single)
  GET    /api/v1/empresas/{id}          detail (scoped)
  GET    /api/v1/empresas/{id}/summary  counts (placeholder, populated by later CRs)
  POST   /api/v1/empresas               admin only
  PATCH  /api/v1/empresas/{id}          admin/manager (manager: own only)
  DELETE /api/v1/empresas/{id}          admin only (soft delete)
  POST   /api/v1/empresas/bulk-import   admin only — CSV/XLSX upload (stub)
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user, require_admin
from app.core.security.scope import apply_scope, can_access_empresa
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.session import get_db
from app.schemas.empresa import (
    EmpresaCreate,
    EmpresaOut,
    EmpresaSummary,
    EmpresaUpdate,
)

router = APIRouter(prefix="/api/v1/empresas", tags=["empresas"])


@router.get(
    "",
    operation_id="listEmpresas",
    response_model=list[EmpresaOut],
    summary="List empresas (scoped). Admin/ops see all; manager sees only theirs.",
)
async def list_empresas(
    q: str | None = Query(default=None, description="search in nombre / rut"),
    active: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EmpresaOut]:
    stmt = select(Empresa)
    stmt = apply_scope(stmt, user, Empresa.empresa_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Empresa.nombre.ilike(like), Empresa.rut.ilike(like)))
    if active is not None:
        stmt = stmt.where(Empresa.activo.is_(active))
    stmt = stmt.order_by(Empresa.empresa_id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return [EmpresaOut.model_validate(e) for e in result.scalars().all()]


@router.get(
    "/me",
    operation_id="getMyEmpresa",
    response_model=EmpresaOut,
    summary="Manager: return your own empresa. 404 for admin/ops (no scope).",
    responses={
        404: {"description": "User has no empresa assigned"},
    },
)
async def get_my_empresa(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaOut:
    if user.empresa_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Your user has no empresa assigned. Falabella staff have no /empresas/me; "
            "use GET /empresas instead.",
        )
    result = await db.execute(
        select(Empresa).where(Empresa.empresa_id == user.empresa_id)
    )
    empresa = result.scalar_one_or_none()
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")
    return EmpresaOut.model_validate(empresa)


@router.get(
    "/{empresa_id}",
    operation_id="getEmpresa",
    response_model=EmpresaOut,
    responses={
        403: {"description": "Out of scope for this user"},
        404: {"description": "Empresa not found"},
    },
)
async def get_empresa(
    empresa_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaOut:
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    result = await db.execute(select(Empresa).where(Empresa.empresa_id == empresa_id))
    empresa = result.scalar_one_or_none()
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")
    return EmpresaOut.model_validate(empresa)


@router.get(
    "/{empresa_id}/summary",
    operation_id="getEmpresaSummary",
    response_model=EmpresaSummary,
    summary="Counts of drivers, vehicles, contactos. Populated as later CRs add those tables.",
)
async def get_empresa_summary(
    empresa_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaSummary:
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    result = await db.execute(select(Empresa).where(Empresa.empresa_id == empresa_id))
    empresa = result.scalar_one_or_none()
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")
    # Vehicles (CR-006), Drivers (CR-007), Contactos (CR-008).
    vehicles_total = (
        await db.execute(
            select(func.count(Vehicle.vehicle_id)).where(
                Vehicle.empresa_id == empresa_id, Vehicle.activo == True  # noqa: E712
            )
        )
    ).scalar_one()
    drivers_total = (
        await db.execute(
            select(func.count(Driver.driver_id)).where(
                Driver.empresa_id == empresa_id, Driver.activo == True  # noqa: E712
            )
        )
    ).scalar_one()
    drivers_opted_in = (
        await db.execute(
            select(func.count(Driver.driver_id)).where(
                Driver.empresa_id == empresa_id,
                Driver.activo == True,  # noqa: E712
                Driver.opted_in_at.is_not(None),
            )
        )
    ).scalar_one()

    contactos_total = (
        await db.execute(
            select(func.count(EmpresaContacto.contact_id)).where(
                EmpresaContacto.empresa_id == empresa_id,
                EmpresaContacto.activo == True,  # noqa: E712
            )
        )
    ).scalar_one()
    contactos_opted_in = (
        await db.execute(
            select(func.count(EmpresaContacto.contact_id)).where(
                EmpresaContacto.empresa_id == empresa_id,
                EmpresaContacto.activo == True,  # noqa: E712
                EmpresaContacto.opted_in_at.is_not(None),
            )
        )
    ).scalar_one()

    return EmpresaSummary(
        empresa_id=empresa.empresa_id,
        nombre=empresa.nombre,
        drivers_total=int(drivers_total),
        drivers_opted_in=int(drivers_opted_in),
        vehicles_total=int(vehicles_total),
        contactos_total=int(contactos_total),
        contactos_opted_in=int(contactos_opted_in),
    )


@router.post(
    "",
    operation_id="createEmpresa",
    response_model=EmpresaOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
    responses={409: {"description": "rut already exists"}},
)
async def create_empresa(
    body: EmpresaCreate,
    db: AsyncSession = Depends(get_db),
) -> EmpresaOut:
    empresa = Empresa(
        nombre=body.nombre,
        razon_social=body.razon_social,
        rut=body.rut,
        central_phone=body.central_phone,
        supervisor_phone_e164=body.supervisor_phone_e164,
    )
    db.add(empresa)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        msg = str(e).lower()
        if "rut" in msg or "unique" in msg or "duplicate" in msg:
            raise HTTPException(status.HTTP_409_CONFLICT, f"rut already exists: {body.rut}") from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"DB constraint: {e}") from None
    await db.refresh(empresa)
    return EmpresaOut.model_validate(empresa)


@router.patch(
    "/{empresa_id}",
    operation_id="updateEmpresa",
    response_model=EmpresaOut,
)
async def update_empresa(
    empresa_id: int,
    body: EmpresaUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaOut:
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    # Only admin can deactivate; manager can only edit fields.
    if body.activo is not None and user.role != "falabella_admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only admin can change 'activo'",
        )

    result = await db.execute(select(Empresa).where(Empresa.empresa_id == empresa_id))
    empresa = result.scalar_one_or_none()
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(empresa, key, value)
    empresa.updated_at = datetime.now(UTC)

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Conflict: {e}") from None
    await db.refresh(empresa)
    return EmpresaOut.model_validate(empresa)


@router.delete(
    "/{empresa_id}",
    operation_id="deactivateEmpresa",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin())],
    summary="Soft-delete (sets activo=false).",
)
async def deactivate_empresa(
    empresa_id: int,
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(select(Empresa).where(Empresa.empresa_id == empresa_id))
    empresa = result.scalar_one_or_none()
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")
    empresa.activo = False
    empresa.updated_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)



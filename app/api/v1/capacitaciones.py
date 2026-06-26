"""Capacitaciones (training) endpoints — nested under drivers."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.db.models.capacitacion import Capacitacion
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.capacitacion import CapacitacionCreate, CapacitacionOut, CapacitacionUpdate

router = APIRouter(prefix="/api/v1/drivers/{driver_id}/capacitaciones", tags=["capacitaciones"])


@router.get("", operation_id="listCapacitaciones", response_model=list[CapacitacionOut])
async def list_capacitaciones(
    driver_id: str,
    _user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CapacitacionOut]:
    result = await db.execute(
        select(Capacitacion)
        .where(Capacitacion.driver_id == driver_id)
        .order_by(Capacitacion.fecha_realizacion.desc())
    )
    return [CapacitacionOut.model_validate(c) for c in result.scalars().all()]


@router.post("", operation_id="createCapacitacion", response_model=CapacitacionOut, status_code=status.HTTP_201_CREATED)
async def create_capacitacion(
    driver_id: str,
    body: CapacitacionCreate,
    _user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CapacitacionOut:
    cap = Capacitacion(driver_id=driver_id, **body.model_dump())
    db.add(cap)
    await db.commit()
    await db.refresh(cap)
    return CapacitacionOut.model_validate(cap)


@router.patch("/{capacitacion_id}", operation_id="updateCapacitacion", response_model=CapacitacionOut)
async def update_capacitacion(
    driver_id: str,
    capacitacion_id: int,
    body: CapacitacionUpdate,
    _user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CapacitacionOut:
    result = await db.execute(
        select(Capacitacion).where(Capacitacion.capacitacion_id == capacitacion_id, Capacitacion.driver_id == driver_id)
    )
    cap = result.scalar_one_or_none()
    if not cap:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capacitacion no encontrada")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cap, k, v)
    await db.commit()
    await db.refresh(cap)
    return CapacitacionOut.model_validate(cap)


@router.delete("/{capacitacion_id}", operation_id="deleteCapacitacion", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capacitacion(
    driver_id: str,
    capacitacion_id: int,
    _user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(
        select(Capacitacion).where(Capacitacion.capacitacion_id == capacitacion_id, Capacitacion.driver_id == driver_id)
    )
    cap = result.scalar_one_or_none()
    if not cap:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capacitacion no encontrada")
    cap.activo = False
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

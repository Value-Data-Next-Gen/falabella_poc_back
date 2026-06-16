"""Motivos de no-entrega CRUD."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user, require_admin
from app.db.models.motivo import Motivo
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.motivo import MotivoCreate, MotivoOut, MotivoUpdate

router = APIRouter(prefix="/api/v1/motivos", tags=["motivos"])


@router.get("", operation_id="listMotivos", response_model=list[MotivoOut])
async def list_motivos(
    _user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MotivoOut]:
    result = await db.execute(select(Motivo).order_by(Motivo.orden, Motivo.motivo_id))
    return [MotivoOut.model_validate(m) for m in result.scalars().all()]


@router.post("", operation_id="createMotivo", response_model=MotivoOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin())])
async def create_motivo(body: MotivoCreate, db: AsyncSession = Depends(get_db)) -> MotivoOut:
    motivo = Motivo(**body.model_dump())
    db.add(motivo)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Motivo '{body.codigo}' ya existe") from None
    await db.refresh(motivo)
    return MotivoOut.model_validate(motivo)


@router.patch("/{motivo_id}", operation_id="updateMotivo", response_model=MotivoOut, dependencies=[Depends(require_admin())])
async def update_motivo(motivo_id: int, body: MotivoUpdate, db: AsyncSession = Depends(get_db)) -> MotivoOut:
    result = await db.execute(select(Motivo).where(Motivo.motivo_id == motivo_id))
    motivo = result.scalar_one_or_none()
    if not motivo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Motivo no encontrado")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(motivo, k, v)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Codigo duplicado") from None
    await db.refresh(motivo)
    return MotivoOut.model_validate(motivo)


@router.delete("/{motivo_id}", operation_id="deleteMotivo", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin())])
async def delete_motivo(motivo_id: int, db: AsyncSession = Depends(get_db)) -> Response:
    result = await db.execute(select(Motivo).where(Motivo.motivo_id == motivo_id))
    motivo = result.scalar_one_or_none()
    if not motivo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Motivo no encontrado")
    motivo.activo = False
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

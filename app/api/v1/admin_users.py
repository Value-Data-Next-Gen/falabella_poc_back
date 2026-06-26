"""CRUD admin endpoints for users.

All endpoints require role `falabella_admin`.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, require_admin
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.session import get_db
from app.schemas.user import UserCreate, UserOut, UserUpdate


async def _user_to_out(db: AsyncSession, user: User) -> UserOut:
    ue_result = await db.execute(select(UserEmpresa.empresa_id).where(UserEmpresa.user_id == user.user_id))
    empresa_ids = [row[0] for row in ue_result.all()]
    out = UserOut.model_validate(user)
    out.empresa_ids = empresa_ids
    return out


async def _sync_empresa_ids(db: AsyncSession, user_id: int, empresa_ids: list[int]) -> None:
    await db.execute(select(UserEmpresa).where(UserEmpresa.user_id == user_id))
    existing = await db.execute(select(UserEmpresa).where(UserEmpresa.user_id == user_id))
    for ue in existing.scalars().all():
        await db.delete(ue)
    for eid in empresa_ids:
        db.add(UserEmpresa(user_id=user_id, empresa_id=eid))

router = APIRouter(
    prefix="/api/v1/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(require_admin())],
)


@router.get(
    "",
    operation_id="listUsers",
    response_model=list[UserOut],
    summary="List users (admin).",
)
async def list_users(
    q: str | None = Query(default=None, description="search in email / display_name"),
    role: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[UserOut]:
    stmt = select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(User.email.ilike(like), User.display_name.ilike(like)))
    if role:
        stmt = stmt.where(User.role == role)
    if active is not None:
        stmt = stmt.where(User.activo.is_(active))
    stmt = stmt.order_by(User.user_id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    users = result.scalars().all()
    return [await _user_to_out(db, u) for u in users]


@router.post(
    "",
    operation_id="createUser",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user (admin).",
    responses={409: {"description": "Email already exists"}},
)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role=body.role,
        empresa_id=body.empresa_ids[0] if body.empresa_ids else body.empresa_id,
        driver_id=body.driver_id,
        phone_e164=body.phone_e164,
        notify_whatsapp=body.notify_whatsapp,
        activation_token=secrets.token_urlsafe(16),
    )
    db.add(user)
    try:
        await db.flush()
        await _sync_empresa_ids(db, user.user_id, body.empresa_ids)
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        if "email" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status.HTTP_409_CONFLICT, f"Email already exists: {body.email}") from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"DB constraint violation: {e}") from None
    await db.refresh(user)
    return await _user_to_out(db, user)


@router.get(
    "/{user_id}",
    operation_id="getUser",
    response_model=UserOut,
    responses={404: {"description": "User not found"}},
)
async def get_user(user_id: int, db: AsyncSession = Depends(get_db)) -> UserOut:
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return await _user_to_out(db, user)


@router.patch(
    "/{user_id}",
    operation_id="updateUser",
    response_model=UserOut,
)
async def update_user(
    user_id: int,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    data = body.model_dump(exclude_unset=True)
    empresa_ids = data.pop("empresa_ids", None)
    if "email" in data:
        data["email"] = data["email"].lower()
    for key, value in data.items():
        setattr(user, key, value)
    user.updated_at = datetime.now(UTC)

    try:
        if empresa_ids is not None:
            await _sync_empresa_ids(db, user.user_id, empresa_ids)
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Conflict: {e}") from None
    await db.refresh(user)
    return await _user_to_out(db, user)


@router.delete(
    "/{user_id}",
    operation_id="deactivateUser",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete (sets activo=false).",
)
async def deactivate_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.activo = False
    user.updated_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""Empresa contactos endpoints — nested under /empresas/{empresa_id}/contactos."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user
from app.core.security.scope import can_access_empresa
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.empresa_contacto import (
    EmpresaContactoCreate,
    EmpresaContactoOut,
    EmpresaContactoUpdate,
    RegenerateContactoActivationResponse,
)

router = APIRouter(prefix="/api/v1/empresas/{empresa_id}/contactos", tags=["empresa-contactos"])


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _gen_token() -> str:
    return secrets.token_urlsafe(16)


def _build_activation_link(token: str) -> str:
    sender = settings.twilio_whatsapp_from.replace("whatsapp:", "").lstrip("+")
    return f"https://wa.me/{sender}?text=ACTIVAR%20{token}"


def _require_can_write(user: User) -> None:
    if user.role not in ("falabella_admin", "falabella_ops", "transport_manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires admin/ops/manager role")


async def _verify_empresa(db: AsyncSession, empresa_id: int) -> None:
    e = (await db.execute(select(Empresa).where(Empresa.empresa_id == empresa_id))).scalar_one_or_none()
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa not found")


async def _get_contacto_or_404(db: AsyncSession, empresa_id: int, contact_id: int) -> EmpresaContacto:
    result = await db.execute(
        select(EmpresaContacto).where(
            EmpresaContacto.contact_id == contact_id,
            EmpresaContacto.empresa_id == empresa_id,
        )
    )
    c = result.scalar_one_or_none()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contacto not found in this empresa")
    return c


# ----------------------------------------------------------------------------
# List
# ----------------------------------------------------------------------------

@router.get(
    "",
    operation_id="listEmpresaContactos",
    response_model=list[EmpresaContactoOut],
)
async def list_contactos(
    empresa_id: int,
    rol: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="search in nombre / phone / email"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EmpresaContactoOut]:
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    await _verify_empresa(db, empresa_id)

    stmt = select(EmpresaContacto).where(EmpresaContacto.empresa_id == empresa_id)
    if rol:
        stmt = stmt.where(EmpresaContacto.rol == rol)
    if active is not None:
        stmt = stmt.where(EmpresaContacto.activo == active)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                EmpresaContacto.nombre.ilike(like),
                EmpresaContacto.phone_e164.ilike(like),
                EmpresaContacto.email.ilike(like),
            )
        )
    stmt = stmt.order_by(EmpresaContacto.contact_id)
    result = await db.execute(stmt)
    return [EmpresaContactoOut.model_validate(c) for c in result.scalars().all()]


# ----------------------------------------------------------------------------
# Get one
# ----------------------------------------------------------------------------

@router.get(
    "/{contact_id}",
    operation_id="getEmpresaContacto",
    response_model=EmpresaContactoOut,
)
async def get_contacto(
    empresa_id: int,
    contact_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaContactoOut:
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    contacto = await _get_contacto_or_404(db, empresa_id, contact_id)
    return EmpresaContactoOut.model_validate(contacto)


# ----------------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------------

@router.post(
    "",
    operation_id="createEmpresaContacto",
    response_model=EmpresaContactoOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Duplicate phone in empresa"},
        404: {"description": "Empresa not found"},
        403: {"description": "Out of scope or insufficient role"},
    },
)
async def create_contacto(
    empresa_id: int,
    body: EmpresaContactoCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaContactoOut:
    _require_can_write(user)
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    await _verify_empresa(db, empresa_id)

    # Check duplicate active phone within empresa.
    if body.phone_e164:
        dup = await db.execute(
            select(EmpresaContacto).where(
                EmpresaContacto.empresa_id == empresa_id,
                EmpresaContacto.phone_e164 == body.phone_e164,
                EmpresaContacto.activo == True,  # noqa: E712
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"phone {body.phone_e164} already exists in empresa {empresa_id}",
            )

    contacto = EmpresaContacto(
        empresa_id=empresa_id,
        nombre=body.nombre,
        rol=body.rol,
        phone_e164=body.phone_e164,
        email=body.email,
        notify_severities=body.notify_severities,
        notify_motivos=body.notify_motivos,
        notes=body.notes,
        activation_token=_gen_token(),
        created_by_user_id=user.user_id,
    )
    db.add(contacto)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"DB constraint: {e}") from None
    await db.refresh(contacto)
    return EmpresaContactoOut.model_validate(contacto)


# ----------------------------------------------------------------------------
# Update
# ----------------------------------------------------------------------------

@router.patch(
    "/{contact_id}",
    operation_id="updateEmpresaContacto",
    response_model=EmpresaContactoOut,
)
async def update_contacto(
    empresa_id: int,
    contact_id: int,
    body: EmpresaContactoUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmpresaContactoOut:
    _require_can_write(user)
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    contacto = await _get_contacto_or_404(db, empresa_id, contact_id)
    if body.activo is not None and user.role == "transport_manager":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager cannot toggle 'activo'")

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(contacto, key, value)
    contacto.updated_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Conflict: {e}") from None
    await db.refresh(contacto)
    return EmpresaContactoOut.model_validate(contacto)


# ----------------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------------

@router.delete(
    "/{contact_id}",
    operation_id="deactivateEmpresaContacto",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deactivate_contacto(
    empresa_id: int,
    contact_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user.role not in ("falabella_admin", "falabella_ops"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admin/ops can deactivate contactos")
    contacto = await _get_contacto_or_404(db, empresa_id, contact_id)
    contacto.activo = False
    contacto.updated_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------
# Regenerate activation token
# ----------------------------------------------------------------------------

@router.post(
    "/{contact_id}/regenerate-activation",
    operation_id="regenerateContactoActivation",
    response_model=RegenerateContactoActivationResponse,
)
async def regenerate_activation(
    empresa_id: int,
    contact_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> RegenerateContactoActivationResponse:
    _require_can_write(user)
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    contacto = await _get_contacto_or_404(db, empresa_id, contact_id)

    new_token = _gen_token()
    contacto.activation_token = new_token
    contacto.activation_used_at = None
    contacto.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(contacto)

    return RegenerateContactoActivationResponse(
        contact_id=contacto.contact_id,
        activation_token=new_token,
        activation_link=_build_activation_link(new_token),
    )

"""Onboarding overview — a single scoped list of everyone who needs to connect
to WhatsApp (conductores + contactos + usuarios), with activation status.

Powers the "Conexión WhatsApp" page so an admin/coordinator can see, in one
place, who is pending and grab each person's invitation link — instead of
hunting through every empresa.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.core.security.scope import apply_scope
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


class OnboardingItem(BaseModel):
    tipo: str                      # conductor | contacto | usuario
    id: str
    nombre: str
    empresa_id: int | None
    empresa_nombre: str | None
    phone_e164: str | None
    activation_token: str | None
    activado: bool


class OnboardingSummary(BaseModel):
    total: int
    activados: int
    pendientes: int
    items: list[OnboardingItem]


@router.get("", operation_id="getOnboarding", response_model=OnboardingSummary)
async def get_onboarding(
    solo_pendientes: bool = Query(default=False, description="Solo no activados"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingSummary:
    """Aggregated, role-scoped activation list. Conductores + contactos are
    scoped to the actor's empresas; usuarios are included only for Falabella
    admin/ops (platform users aren't empresa-scoped)."""
    empresas = {
        eid: nombre
        for eid, nombre in (await db.execute(select(Empresa.empresa_id, Empresa.nombre))).all()
    }
    items: list[OnboardingItem] = []

    drivers = (await db.execute(
        apply_scope(select(Driver).where(Driver.activo.is_(True)), user, Driver.empresa_id)
    )).scalars().all()
    items += [
        OnboardingItem(
            tipo="conductor", id=d.driver_id, nombre=d.nombre, empresa_id=d.empresa_id,
            empresa_nombre=empresas.get(d.empresa_id), phone_e164=d.phone_e164,
            activation_token=d.activation_token, activado=d.opted_in_at is not None,
        ) for d in drivers
    ]

    contactos = (await db.execute(
        apply_scope(select(EmpresaContacto).where(EmpresaContacto.activo.is_(True)),
                    user, EmpresaContacto.empresa_id)
    )).scalars().all()
    items += [
        OnboardingItem(
            tipo="contacto", id=str(c.contact_id), nombre=c.nombre, empresa_id=c.empresa_id,
            empresa_nombre=empresas.get(c.empresa_id), phone_e164=c.phone_e164,
            activation_token=c.activation_token, activado=c.opted_in_at is not None,
        ) for c in contactos
    ]

    if user.role in ("falabella_admin", "falabella_ops"):
        users = (await db.execute(select(User).where(User.activo.is_(True)))).scalars().all()
        items += [
            OnboardingItem(
                tipo="usuario", id=str(u.user_id), nombre=u.display_name, empresa_id=u.empresa_id,
                empresa_nombre=empresas.get(u.empresa_id) if u.empresa_id else None,
                phone_e164=u.phone_e164, activation_token=u.activation_token,
                activado=u.activation_used_at is not None,
            ) for u in users
        ]

    activados = sum(1 for i in items if i.activado)
    total = len(items)
    if solo_pendientes:
        items = [i for i in items if not i.activado]
    # pendientes first, then by empresa, then by name
    items.sort(key=lambda i: (i.activado, i.empresa_nombre or "~", i.nombre or ""))

    return OnboardingSummary(
        total=total, activados=activados, pendientes=total - activados, items=items,
    )

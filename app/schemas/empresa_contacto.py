"""Pydantic schemas for EmpresaContacto entity."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

ContactoRol = Literal["jefe", "coordinador", "otro"]


class EmpresaContactoBase(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    rol: ContactoRol
    phone_e164: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = Field(default=None, max_length=200)
    notify_severities: str | None = Field(default=None, max_length=100)
    notify_motivos: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=500)


class EmpresaContactoCreate(EmpresaContactoBase):
    pass


class EmpresaContactoUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=200)
    rol: ContactoRol | None = None
    phone_e164: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = Field(default=None, max_length=200)
    notify_severities: str | None = Field(default=None, max_length=100)
    notify_motivos: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=500)
    activo: bool | None = None


class EmpresaContactoOut(EmpresaContactoBase):
    contact_id: int
    empresa_id: int
    opted_in_at: datetime | None = None
    activation_token: str | None = None
    activation_used_at: datetime | None = None
    activo: bool
    created_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RegenerateContactoActivationResponse(BaseModel):
    contact_id: int
    activation_token: str
    activation_link: str

"""Pydantic schemas for User entity."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

UserRole = Literal["falabella_admin", "falabella_ops", "transport_manager", "driver"]


class UserBase(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    role: UserRole
    empresa_id: int | None = None
    driver_id: str | None = Field(default=None, max_length=20)
    phone_e164: str | None = Field(default=None, max_length=20)
    notify_whatsapp: bool = False


class UserCreate(UserBase):
    password: str = Field(min_length=4, max_length=128)
    empresa_ids: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: UserRole | None = None
    empresa_id: int | None = None
    empresa_ids: list[int] | None = None
    driver_id: str | None = Field(default=None, max_length=20)
    phone_e164: str | None = Field(default=None, max_length=20)
    notify_whatsapp: bool | None = None
    activo: bool | None = None


class UserOut(UserBase):
    user_id: int
    activo: bool
    activation_token: str | None = None
    activation_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_login: datetime | None = None
    empresa_ids: list[int] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)

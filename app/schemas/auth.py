"""Pydantic schemas for auth flow."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserOut


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    """Cookie is set in the response; the body returns the user payload."""

    user: UserOut

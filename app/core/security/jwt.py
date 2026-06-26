"""JWT encode / decode + cookie helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
from fastapi import Response

from app.core.config import settings

JWT_ALG = "HS256"
COOKIE_NAME = "td_session"


def encode_token(*, user_id: int, role: str, ttl_hours: int | None = None) -> tuple[str, datetime]:
    """Return (token, expiry_utc)."""
    ttl = ttl_hours if ttl_hours is not None else settings.jwt_ttl_hours
    now = datetime.now(UTC)
    exp = now + timedelta(hours=ttl)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = pyjwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALG)
    return token, exp


def decode_token(token: str) -> dict[str, Any]:
    """Raises `pyjwt.PyJWTError` on invalid / expired."""
    return pyjwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=[JWT_ALG])


def set_session_cookie(response: Response, token: str, ttl_hours: int | None = None) -> None:
    """Attach the JWT to the response as an httpOnly cookie."""
    ttl = ttl_hours if ttl_hours is not None else settings.jwt_ttl_hours
    max_age = ttl * 3600
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,   # True in prod (HTTPS); COOKIE_SECURE=false for local http dev
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")

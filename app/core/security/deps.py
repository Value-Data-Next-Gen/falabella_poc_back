"""FastAPI dependencies: `current_user` and `require_role`."""
from __future__ import annotations

from typing import Annotated

import jwt as pyjwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security.jwt import decode_token
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.session import get_db


async def current_user(
    td_session: Annotated[str | None, Cookie()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Validates `td_session` cookie, returns the User row.

    Raises 401 if cookie missing / invalid / expired / user deactivated.
    """
    if not td_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(td_session)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from None
    except pyjwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session") from None

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session payload")
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session payload") from None

    # Single round-trip: user + their empresa_ids via LEFT JOIN (was 2 queries
    # on every authenticated request).
    rows = (await db.execute(
        select(User, UserEmpresa.empresa_id)
        .outerjoin(UserEmpresa, UserEmpresa.user_id == User.user_id)
        .where(User.user_id == user_id)
    )).all()
    user = rows[0][0] if rows else None
    if user is None or not user.activo:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or deactivated")
    user._empresa_ids = [r[1] for r in rows if r[1] is not None]  # type: ignore[attr-defined]
    return user


def require_role(*roles: str):
    """Dependency factory: returns a dep that 403s if user role not in `roles`."""

    async def _check(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires role in {sorted(roles)}; got '{user.role}'",
            )
        return user

    return _check


def require_admin():
    return require_role("falabella_admin")


def require_admin_or_ops():
    return require_role("falabella_admin", "falabella_ops")

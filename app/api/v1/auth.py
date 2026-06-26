"""Auth endpoints: login / logout / me."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    clear_session_cookie,
    current_user,
    encode_token,
    set_session_cookie,
    verify_password,
)
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.auth import LoginRequest, LoginResponse
from app.schemas.user import UserOut

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/login",
    operation_id="login",
    response_model=LoginResponse,
    summary="Login with email + password; sets td_session cookie.",
)
async def login(
    req: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not user.activo:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    token, _exp = encode_token(user_id=user.user_id, role=user.role)
    set_session_cookie(response, token)

    user.last_login = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)

    return LoginResponse(user=UserOut.model_validate(user))


@router.post(
    "/logout",
    operation_id="logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear session cookie.",
)
async def logout(response: Response, _user: User = Depends(current_user)) -> Response:
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/me",
    operation_id="getMe",
    response_model=UserOut,
    summary="Return the currently-authenticated user.",
)
async def get_me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)

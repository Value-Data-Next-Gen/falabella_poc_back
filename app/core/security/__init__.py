"""Auth + scope helpers."""
from app.core.security.deps import (
    current_user,
    require_admin,
    require_admin_or_ops,
    require_role,
)
from app.core.security.jwt import (
    COOKIE_NAME,
    clear_session_cookie,
    decode_token,
    encode_token,
    set_session_cookie,
)
from app.core.security.passwords import hash_password, verify_password

__all__ = [
    "COOKIE_NAME",
    "clear_session_cookie",
    "current_user",
    "decode_token",
    "encode_token",
    "hash_password",
    "require_admin",
    "require_admin_or_ops",
    "require_role",
    "set_session_cookie",
    "verify_password",
]

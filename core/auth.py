"""Auth multi-tenant para la torre de control.

- Login: POST /api/auth/login {email, password} -> JWT
- Me:    GET  /api/auth/me (Bearer)
- Empresas: GET /api/empresas (solo admin/ops)
- Dependency `current_user` decodifica JWT y carga user + empresa.
- `apply_scope(df, user)` filtra por empresa para transport_manager.

Credenciales DB se leen de os.environ (DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD, DB_DRIVER).
En Azure App Service vienen como Application Settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from passlib.hash import bcrypt
from pydantic import BaseModel, EmailStr

from core.db import get_conn


JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
JWT_ALGO = "HS256"
JWT_TTL_HOURS = int(os.environ.get("JWT_TTL_HOURS", "12"))

# Fail-fast (CR fixes-qa H5): no permitimos arrancar con JWT_SECRET vacía o
# con el placeholder histórico "dev-secret-change-me-for-prod" — los tokens
# emitidos serían triviales de forjar. Si necesitás levantar local sin .env,
# setealo en el shell antes de uvicorn (`export JWT_SECRET=...`).
_LEGACY_DEFAULT = "dev-secret-change-me-for-prod"
if not JWT_SECRET or JWT_SECRET == _LEGACY_DEFAULT:
    raise RuntimeError(
        "JWT_SECRET no configurado: setear env var con un secreto fuerte "
        "(>=32 chars, random). Ejemplo: "
        "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` y "
        "pegarlo en .env / Azure App Settings."
    )

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api/auth", tags=["auth"])
empresas_router = APIRouter(prefix="/api/empresas", tags=["empresas"])


def _db():
    return get_conn()


# ---------- Modelos ----------
@dataclass
class CurrentUser:
    user_id: int
    email: str
    display_name: str
    role: str
    empresa_id: Optional[int]
    empresa_nombre: Optional[str]
    driver_id: Optional[str] = None
    # CR-011: phone real del user. Lo usa el agente web para reusar la
    # detección por teléfono del FSM de WhatsApp (mismo flujo bi-canal).
    phone_e164: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == "falabella_admin"

    @property
    def is_falabella(self) -> bool:
        return self.role in ("falabella_admin", "falabella_ops")

    @property
    def is_driver(self) -> bool:
        return self.role == "driver"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "MeResponse"


class MeResponse(BaseModel):
    user_id: int
    email: str
    display_name: str
    role: str
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    driver_id: Optional[str] = None


class EmpresaResponse(BaseModel):
    empresa_id: int
    nombre: str
    activo: bool


LoginResponse.model_rebuild()


# ---------- JWT ----------
def create_token(user_id: int, email: str, role: str, empresa_id: Optional[int]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "empresa_id": empresa_id,
        "iat": now,
        "exp": now + timedelta(hours=JWT_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")


# ---------- Data access ----------
def _load_user_by_email(email: str) -> Optional[dict]:
    with _db() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT u.user_id, u.email, u.password_hash, u.display_name, u.role,
                   u.empresa_id, u.driver_id, u.activo, e.nombre AS empresa_nombre
            FROM fpoc.users u
            LEFT JOIN fpoc.empresas_transporte e ON u.empresa_id = e.empresa_id
            WHERE u.email = ?
            """,
            email,
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": int(row.user_id),
            "email": row.email,
            "password_hash": row.password_hash,
            "display_name": row.display_name,
            "role": row.role,
            "empresa_id": int(row.empresa_id) if row.empresa_id is not None else None,
            "empresa_nombre": row.empresa_nombre,
            "driver_id": str(row.driver_id) if row.driver_id is not None else None,
            "activo": bool(row.activo),
        }


def _load_user_by_id(user_id: int) -> Optional[dict]:
    with _db() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT u.user_id, u.email, u.display_name, u.role,
                   u.empresa_id, u.driver_id, u.activo, u.phone_e164,
                   e.nombre AS empresa_nombre
            FROM fpoc.users u
            LEFT JOIN fpoc.empresas_transporte e ON u.empresa_id = e.empresa_id
            WHERE u.user_id = ?
            """,
            user_id,
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": int(row.user_id),
            "email": row.email,
            "display_name": row.display_name,
            "role": row.role,
            "empresa_id": int(row.empresa_id) if row.empresa_id is not None else None,
            "empresa_nombre": row.empresa_nombre,
            "driver_id": str(row.driver_id) if row.driver_id is not None else None,
            "activo": bool(row.activo),
            "phone_e164": str(row.phone_e164) if row.phone_e164 is not None else None,
        }


def _extract_request_meta(request: Request | None) -> tuple[Optional[str], Optional[str]]:
    """Obtiene IP y user-agent. Prioriza X-Forwarded-For (ngrok / reverse proxy)."""
    if request is None:
        return None, None
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    ua = (request.headers.get("user-agent") or "")[:500] or None
    return ip, ua


def _log_access(
    event_type: str,
    *,
    user_id: Optional[int] = None,
    email: Optional[str] = None,
    ip: Optional[str] = None,
    ua: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Registra un evento de acceso. Nunca rompe el flujo si falla."""
    try:
        with _db() as cn:
            cn.cursor().execute(
                """INSERT INTO fpoc.access_log
                     (event_type, user_id, email_attempted, ip_address, user_agent, error_detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                event_type, user_id, email, ip, ua, error,
            )
            cn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[access-log] falló registrar {event_type}: {e}")


def _touch_last_login(user_id: int) -> None:
    with _db() as cn:
        cur = cn.cursor()
        cur.execute("UPDATE fpoc.users SET last_login = CURRENT_TIMESTAMP WHERE user_id = ?", user_id)
        cn.commit()


# ---------- Dependency ----------
def current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    if not creds or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta Authorization Bearer")
    payload = decode_token(creds.credentials)
    try:
        uid = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token sin sub")
    u = _load_user_by_id(uid)
    if not u or not u["activo"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inactivo o inexistente")
    return CurrentUser(
        user_id=u["user_id"], email=u["email"], display_name=u["display_name"],
        role=u["role"], empresa_id=u["empresa_id"], empresa_nombre=u["empresa_nombre"],
        driver_id=u.get("driver_id"),
        phone_e164=u.get("phone_e164"),
    )


def require_admin(u: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not u.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requiere rol falabella_admin")
    return u


# ---------- Scope helper ----------
def apply_scope(df: pd.DataFrame, user: CurrentUser, empresa_col: str = "empresa_id") -> pd.DataFrame:
    """Si el usuario es transport_manager, filtra el df a su empresa."""
    if user.is_falabella:
        return df
    if empresa_col not in df.columns or user.empresa_id is None:
        return df.iloc[0:0]  # sin scope válido → vacío
    return df[df[empresa_col] == user.empresa_id]


# ---------- Endpoints ----------
@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request) -> LoginResponse:
    ip, ua = _extract_request_meta(request)
    email = req.email.lower()
    u = _load_user_by_email(email)
    if not u or not u["activo"]:
        _log_access("login_failed", email=email, ip=ip, ua=ua,
                    error="user not found or inactive")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")
    try:
        ok = bcrypt.verify(req.password, u["password_hash"]) if u["password_hash"] else False
    except Exception as e:  # noqa: BLE001
        # Hash mal formado / NULL / encoding roto -> tratar como credencial invalida
        # (no spamear 500). Ver logs del backend para diagnostico (auth.bcrypt).
        logger.warning(
            f"[auth] bcrypt.verify fallo para user_id={u['user_id']} email={email}: "
            f"{type(e).__name__}: {e} (hash_type={type(u['password_hash']).__name__} "
            f"hash_len={len(u['password_hash']) if u['password_hash'] else 0})"
        )
        ok = False
    if not ok:
        _log_access("login_failed", user_id=u["user_id"], email=email, ip=ip, ua=ua,
                    error="wrong password or hash error")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")

    _log_access("login_success", user_id=u["user_id"], email=u["email"], ip=ip, ua=ua)
    _touch_last_login(u["user_id"])
    token = create_token(u["user_id"], u["email"], u["role"], u["empresa_id"])
    me = MeResponse(
        user_id=u["user_id"], email=u["email"], display_name=u["display_name"],
        role=u["role"], empresa_id=u["empresa_id"], empresa_nombre=u["empresa_nombre"],
        driver_id=u.get("driver_id"),
    )
    return LoginResponse(access_token=token, user=me)


@router.get("/me", response_model=MeResponse)
def me(u: CurrentUser = Depends(current_user)) -> MeResponse:
    return MeResponse(
        user_id=u.user_id, email=u.email, display_name=u.display_name,
        role=u.role, empresa_id=u.empresa_id, empresa_nombre=u.empresa_nombre,
    )


class AccessLogRow(BaseModel):
    log_id: int
    event_type: str
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    user_display_name: Optional[str] = None
    user_role: Optional[str] = None
    email_attempted: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    error_detail: Optional[str] = None
    created_at: str


@router.get("/access-log", response_model=list[AccessLogRow])
def get_access_log(
    limit: int = Query(default=100, ge=1, le=500),
    event_type: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(require_admin),
) -> list[AccessLogRow]:
    params: list = []
    where = ""
    if event_type:
        where = " WHERE l.event_type = ?"
        params.append(event_type)
    # `limit` viene de Pydantic Query(ge=1, le=500) → safe to inline.
    # db.py traduce "LIMIT N" → "SELECT TOP N" en sqlserver, pero el regex sólo
    # matchea literales, no placeholders.
    with _db() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT l.log_id, l.event_type, l.user_id,
                   u.email AS user_email, u.display_name AS user_display_name,
                   u.role AS user_role, l.email_attempted, l.ip_address,
                   l.user_agent, l.error_detail, l.created_at
            FROM fpoc.access_log l
            LEFT JOIN fpoc.users u ON u.user_id = l.user_id
            {where}
            ORDER BY l.created_at DESC
            LIMIT {int(limit)}
            """,
            *params,
        )
        rows = cur.fetchall()
    return [
        AccessLogRow(
            log_id=int(r.log_id),
            event_type=r.event_type,
            user_id=int(r.user_id) if r.user_id is not None else None,
            user_email=r.user_email,
            user_display_name=r.user_display_name,
            user_role=r.user_role,
            email_attempted=r.email_attempted,
            ip_address=r.ip_address,
            user_agent=r.user_agent,
            error_detail=r.error_detail,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


class AccessSummary(BaseModel):
    total_24h: int
    success_24h: int
    failed_24h: int
    unique_users_24h: int
    unique_ips_24h: int


@router.get("/access-summary", response_model=AccessSummary)
def get_access_summary(user: CurrentUser = Depends(require_admin)) -> AccessSummary:
    with _db() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN event_type='login_success' THEN 1 ELSE 0 END) AS ok,
              SUM(CASE WHEN event_type='login_failed' THEN 1 ELSE 0 END) AS fail,
              COUNT(DISTINCT user_id) AS users,
              COUNT(DISTINCT ip_address) AS ips
            FROM fpoc.access_log
            WHERE created_at >= datetime('now', '-24 hours')
            """
        )
        r = cur.fetchone()
    return AccessSummary(
        total_24h=int(r.total or 0),
        success_24h=int(r.ok or 0),
        failed_24h=int(r.fail or 0),
        unique_users_24h=int(r.users or 0),
        unique_ips_24h=int(r.ips or 0),
    )


@empresas_router.get("", response_model=list[EmpresaResponse])
def list_empresas(u: CurrentUser = Depends(current_user)) -> list[EmpresaResponse]:
    with _db() as cn:
        cur = cn.cursor()
        if u.is_falabella:
            cur.execute("SELECT empresa_id, nombre, activo FROM fpoc.empresas_transporte ORDER BY empresa_id")
        else:
            cur.execute(
                "SELECT empresa_id, nombre, activo FROM fpoc.empresas_transporte WHERE empresa_id = ?",
                u.empresa_id,
            )
        return [EmpresaResponse(empresa_id=int(r.empresa_id), nombre=r.nombre, activo=bool(r.activo))
                for r in cur.fetchall()]

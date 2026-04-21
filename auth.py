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
import pyodbc
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.hash import bcrypt
from pydantic import BaseModel, EmailStr


JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me-for-prod")
JWT_ALGO = "HS256"
JWT_TTL_HOURS = int(os.environ.get("JWT_TTL_HOURS", "12"))

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api/auth", tags=["auth"])
empresas_router = APIRouter(prefix="/api/empresas", tags=["empresas"])


# ---------- Conexión SQL ----------
def _db_conn_str() -> str:
    return (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={os.environ['DB_SERVER'].replace('tcp:', '')};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )


def _db() -> pyodbc.Connection:
    return pyodbc.connect(_db_conn_str(), autocommit=False)


# ---------- Modelos ----------
@dataclass
class CurrentUser:
    user_id: int
    email: str
    display_name: str
    role: str
    empresa_id: Optional[int]
    empresa_nombre: Optional[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "falabella_admin"

    @property
    def is_falabella(self) -> bool:
        return self.role in ("falabella_admin", "falabella_ops")


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
                   u.empresa_id, u.activo, e.nombre AS empresa_nombre
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
            "activo": bool(row.activo),
        }


def _load_user_by_id(user_id: int) -> Optional[dict]:
    with _db() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT u.user_id, u.email, u.display_name, u.role,
                   u.empresa_id, u.activo, e.nombre AS empresa_nombre
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
            "activo": bool(row.activo),
        }


def _touch_last_login(user_id: int) -> None:
    with _db() as cn:
        cur = cn.cursor()
        cur.execute("UPDATE fpoc.users SET last_login = SYSUTCDATETIME() WHERE user_id = ?", user_id)
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
def login(req: LoginRequest) -> LoginResponse:
    u = _load_user_by_email(req.email.lower())
    if not u or not u["activo"] or not bcrypt.verify(req.password, u["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")
    _touch_last_login(u["user_id"])
    token = create_token(u["user_id"], u["email"], u["role"], u["empresa_id"])
    me = MeResponse(
        user_id=u["user_id"], email=u["email"], display_name=u["display_name"],
        role=u["role"], empresa_id=u["empresa_id"], empresa_nombre=u["empresa_nombre"],
    )
    return LoginResponse(access_token=token, user=me)


@router.get("/me", response_model=MeResponse)
def me(u: CurrentUser = Depends(current_user)) -> MeResponse:
    return MeResponse(
        user_id=u.user_id, email=u.email, display_name=u.display_name,
        role=u.role, empresa_id=u.empresa_id, empresa_nombre=u.empresa_nombre,
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

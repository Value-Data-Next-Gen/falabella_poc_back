"""CRUD admin de usuarios. Extraído de mantenedores.py en R7-F4.

URLs (todas bajo el prefix /api/admin que añade el router padre):
  GET    /api/admin/users
  POST   /api/admin/users
  PUT    /api/admin/users/{user_id}
  POST   /api/admin/users/{user_id}/reset-password
  DELETE /api/admin/users/{user_id}

Reglas:
  - admin / falabella_ops: ven y gestionan todos.
  - transport_manager: solo ve usuarios de su empresa y solo puede crear/
    editar drivers de su empresa.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from passlib.hash import bcrypt
from pydantic import BaseModel, EmailStr, Field

from core.activation import build_activation_link, gen_activation_token
from core.auth import CurrentUser, current_user, require_admin
from core.db import get_conn


router = APIRouter(tags=["admin-maestros"])


ALLOWED_ROLES = {"falabella_admin", "falabella_ops", "transport_manager"}


class UserIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(falabella_admin|falabella_ops|transport_manager|driver)$")
    empresa_id: Optional[int] = None
    driver_id: Optional[str] = Field(default=None, max_length=20)
    activo: bool = True
    phone_e164: Optional[str] = Field(default=None, max_length=20)
    notify_whatsapp: bool = False


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    role: Optional[str] = Field(default=None, pattern="^(falabella_admin|falabella_ops|transport_manager|driver)$")
    empresa_id: Optional[int] = None
    driver_id: Optional[str] = Field(default=None, max_length=20)
    activo: Optional[bool] = None
    phone_e164: Optional[str] = Field(default=None, max_length=20)
    notify_whatsapp: Optional[bool] = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=4, max_length=128)


class UserOut(BaseModel):
    user_id: int
    email: str
    display_name: str
    role: str
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    activo: bool
    phone_e164: Optional[str] = None
    notify_whatsapp: bool
    created_at: Optional[str] = None
    last_login: Optional[str] = None
    # CR-014: activation token (wa.me link workaround para error 63112 de Meta).
    activation_token: Optional[str] = None
    activation_link: Optional[str] = None
    activation_used_at: Optional[str] = None


class ActivationLinkOut(BaseModel):
    token: str
    link: str
    used_at: Optional[str] = None
    is_used: bool


def _user_row(r) -> UserOut:
    def _iso(v):
        if v is None: return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
    token = getattr(r, "activation_token", None)
    return UserOut(
        user_id=int(r.user_id), email=r.email, display_name=r.display_name,
        role=r.role,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        empresa_nombre=r.empresa_nombre,
        driver_id=str(r.driver_id) if r.driver_id is not None else None,
        driver_name=getattr(r, "driver_name", None),
        activo=bool(r.activo),
        phone_e164=r.phone_e164,
        notify_whatsapp=bool(r.notify_whatsapp),
        created_at=_iso(r.created_at),
        last_login=_iso(r.last_login),
        activation_token=token,
        activation_link=build_activation_link(token) if token else None,
        activation_used_at=_iso(getattr(r, "activation_used_at", None)),
    )


_USER_SELECT = """
    SELECT u.user_id, u.email, u.display_name, u.role, u.empresa_id,
           u.driver_id, u.activo, u.phone_e164, u.notify_whatsapp,
           u.created_at, u.last_login,
           u.activation_token, u.activation_used_at,
           e.nombre AS empresa_nombre,
           d.name AS driver_name
    FROM fpoc.users u
    LEFT JOIN fpoc.empresas_transporte e ON u.empresa_id = e.empresa_id
    LEFT JOIN fpoc.drivers d ON u.driver_id = d.driver_id
"""


def _can_manage_user(actor: CurrentUser, target_role: str, target_empresa_id: Optional[int]) -> bool:
    """admin: todos. transport_manager: solo crear/editar drivers de su empresa."""
    if actor.is_admin:
        return True
    if actor.role == "transport_manager" and target_role == "driver":
        return target_empresa_id == actor.empresa_id
    return False


@router.get("/users", response_model=list[UserOut])
def list_users(user: CurrentUser = Depends(current_user)) -> list[UserOut]:
    """admin: todos. transport_manager: solo users de su empresa
    (típicamente él mismo + drivers de su empresa con cuenta)."""
    if user.is_admin or user.role == "falabella_ops":
        where = ""
        params: list = []
    elif user.role == "transport_manager" and user.empresa_id is not None:
        where = " WHERE u.empresa_id = ?"
        params = [user.empresa_id]
    else:
        raise HTTPException(403, "Sin permisos para listar usuarios")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(_USER_SELECT + where + " ORDER BY u.user_id", *params)
        return [_user_row(r) for r in cur.fetchall()]


@router.post("/users", response_model=UserOut)
def create_user(req: UserIn, user: CurrentUser = Depends(current_user)) -> UserOut:
    if not _can_manage_user(user, req.role, req.empresa_id):
        raise HTTPException(403, "Sin permisos para crear este usuario")
    if req.role == "transport_manager" and req.empresa_id is None:
        raise HTTPException(400, "transport_manager requiere empresa_id")
    if req.role == "driver":
        if req.driver_id is None or req.empresa_id is None:
            raise HTTPException(400, "rol driver requiere driver_id y empresa_id")
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("SELECT empresa_id FROM fpoc.drivers WHERE driver_id = ?", req.driver_id)
            row = cur.fetchone()
            if not row:
                raise HTTPException(400, f"driver_id {req.driver_id} no existe")
            if int(row.empresa_id) != req.empresa_id:
                raise HTTPException(400, "driver_id no pertenece a la empresa indicada")
    email_lower = req.email.lower()
    # CR-014: token de activación para evitar el bloqueo 63112 de Meta (wa.me link).
    activation_token = gen_activation_token()
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO fpoc.users
                  (email, password_hash, display_name, role, empresa_id, driver_id, activo,
                   phone_e164, notify_whatsapp, activation_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                email_lower, bcrypt.hash(req.password), req.display_name,
                req.role, req.empresa_id, req.driver_id, 1 if req.activo else 0,
                req.phone_e164, 1 if req.notify_whatsapp else 0,
                activation_token,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"email duplicado o datos inválidos: {e}")
        cur.execute(_USER_SELECT + " WHERE u.email = ?", email_lower)
        return _user_row(cur.fetchone())


@router.get("/users/{user_id}/activation-link", response_model=ActivationLinkOut)
def get_user_activation_link(
    user_id: int,
    _: CurrentUser = Depends(require_admin),
) -> ActivationLinkOut:
    """Devuelve (o regenera) el wa.me activation link de un user.

    Si el token está usado (activation_used_at IS NOT NULL) se regenera uno
    nuevo y se limpia used_at — el admin puede reenviar el link cuando quiera.
    Solo admin/ops pueden llamar (require_admin gatea falabella_admin).
    """
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT activation_token, activation_used_at FROM fpoc.users WHERE user_id = ?",
            user_id,
        )
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, "user no encontrado")
        token = getattr(r, "activation_token", None)
        used_at = getattr(r, "activation_used_at", None)
        if not token or used_at is not None:
            token = gen_activation_token()
            cur.execute(
                "UPDATE fpoc.users SET activation_token = ?, activation_used_at = NULL "
                "WHERE user_id = ?",
                token, user_id,
            )
            cn.commit()
            used_at = None
    used_iso = used_at.isoformat() if hasattr(used_at, "isoformat") else (str(used_at) if used_at else None)
    return ActivationLinkOut(
        token=token,
        link=build_activation_link(token),
        used_at=used_iso,
        is_used=used_at is not None,
    )


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, req: UserUpdate,
                _: CurrentUser = Depends(require_admin)) -> UserOut:
    sets, params = [], []
    for field, col in [
        ("email", "email"), ("display_name", "display_name"),
        ("role", "role"), ("empresa_id", "empresa_id"),
        ("phone_e164", "phone_e164"),
    ]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{col} = ?")
            params.append(v.lower() if field == "email" else v)
    if req.activo is not None:
        sets.append("activo = ?"); params.append(1 if req.activo else 0)
    if req.notify_whatsapp is not None:
        sets.append("notify_whatsapp = ?"); params.append(1 if req.notify_whatsapp else 0)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(user_id)
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(f"UPDATE fpoc.users SET {', '.join(sets)} WHERE user_id = ?", *params)
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"datos inválidos: {e}")
        if cur.rowcount == 0:
            raise HTTPException(404, "user no encontrado")
        cn.commit()
        cur.execute(_USER_SELECT + " WHERE u.user_id = ?", user_id)
        return _user_row(cur.fetchone())


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, req: PasswordReset,
                   _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE fpoc.users SET password_hash = ? WHERE user_id = ?",
            bcrypt.hash(req.new_password), user_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "user no encontrado")
        cn.commit()
    return {"reset": user_id}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: CurrentUser = Depends(require_admin)) -> dict:
    if user_id == admin.user_id:
        raise HTTPException(400, "no puedes eliminarte a ti mismo")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.users WHERE user_id = ?", user_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "user no encontrado")
        cn.commit()
    return {"deleted": user_id}

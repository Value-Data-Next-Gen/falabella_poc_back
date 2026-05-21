"""CRUD admin de drivers. Extraído de mantenedores.py en R7-F4.

URLs (todas bajo el prefix /api/admin que añade el router padre):
  GET    /api/admin/drivers
  POST   /api/admin/drivers
  PUT    /api/admin/drivers/{driver_id}
  DELETE /api/admin/drivers/{driver_id}

Permisos via require_fleet_access: admin/ops o transport_manager scopeado
a su empresa. Tras cada mutación se llama refresh_state_maestros().
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.activation import build_activation_link, gen_activation_token
from core.auth import CurrentUser
from core.db import get_conn
from routers.mantenedores_shared import (
    enforce_fleet_empresa,
    refresh_state_maestros,
    require_fleet_access,
)


router = APIRouter(tags=["admin-maestros"])


class DriverIn(BaseModel):
    driver_id: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=8, max_length=50)
    license: Optional[str] = Field(default="A-3 Profesional", max_length=50)
    empresa_id: int = Field(ge=1)
    vehicle_id: int = Field(ge=1)
    vehicle_name: str = Field(min_length=1, max_length=50)
    rating: float = Field(default=4.5, ge=0.0, le=5.0)
    deliveries_30d: int = Field(default=0, ge=0)
    fail_rate_30d: float = Field(default=0.10, ge=0.0, le=1.0)
    joined_at: Optional[date] = None
    active: bool = True


class DriverUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=50)
    license: Optional[str] = Field(default=None, max_length=50)
    empresa_id: Optional[int] = Field(default=None, ge=1)
    vehicle_id: Optional[int] = Field(default=None, ge=1)
    vehicle_name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    rating: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    deliveries_30d: Optional[int] = Field(default=None, ge=0)
    fail_rate_30d: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    joined_at: Optional[date] = None
    active: Optional[bool] = None


class DriverOut(BaseModel):
    driver_id: str
    name: str
    phone: Optional[str] = None
    license: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    vehicle_id: int
    vehicle_name: str
    rating: float
    deliveries_30d: int
    fail_rate_30d: float
    joined_at: Optional[str] = None
    active: bool
    is_problem_hidden: bool = False
    # CR-014: activation token (wa.me link workaround para error 63112 de Meta).
    activation_token: Optional[str] = None
    activation_link: Optional[str] = None
    activation_used_at: Optional[str] = None


class ActivationLinkOut(BaseModel):
    token: str
    link: str
    used_at: Optional[str] = None
    is_used: bool


def _driver_row(r) -> DriverOut:
    joined = r.joined_at
    token = getattr(r, "activation_token", None)
    used_at = getattr(r, "activation_used_at", None)
    used_iso = used_at.isoformat() if hasattr(used_at, "isoformat") else (str(used_at) if used_at else None)
    return DriverOut(
        driver_id=r.driver_id, name=r.name, phone=r.phone, license=r.license,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        empresa_nombre=getattr(r, "empresa_nombre", None),
        vehicle_id=int(r.vehicle_id), vehicle_name=r.vehicle_name,
        rating=float(r.rating), deliveries_30d=int(r.deliveries_30d),
        fail_rate_30d=float(r.fail_rate_30d),
        joined_at=joined.isoformat() if hasattr(joined, "isoformat") else (joined or None),
        active=bool(r.active),
        is_problem_hidden=bool(r.is_problem_hidden),
        activation_token=token,
        activation_link=build_activation_link(token) if token else None,
        activation_used_at=used_iso,
    )


def fetch_driver(driver_id: str) -> DriverOut:
    """Lee un driver por id. Helper público para uso de otros sub-módulos."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT d.driver_id, d.name, d.phone, d.license, d.empresa_id,
                       e.nombre AS empresa_nombre,
                       d.vehicle_id, d.vehicle_name,
                       d.rating, d.deliveries_30d, d.fail_rate_30d, d.joined_at, d.active,
                       d.is_problem_hidden,
                       d.activation_token, d.activation_used_at
                FROM fpoc.drivers d
                LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id
                WHERE d.driver_id = ?""",
            driver_id,
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "driver no encontrado")
        return _driver_row(r)


@router.get("/drivers", response_model=list[DriverOut])
def list_drivers(user: CurrentUser = Depends(require_fleet_access)) -> list[DriverOut]:
    where = "" if user.is_falabella else "WHERE d.empresa_id = ?"
    params: list = [] if user.is_falabella else [user.empresa_id]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT d.driver_id, d.name, d.phone, d.license, d.empresa_id,
                       e.nombre AS empresa_nombre,
                       d.vehicle_id, d.vehicle_name,
                       d.rating, d.deliveries_30d, d.fail_rate_30d, d.joined_at, d.active,
                       d.is_problem_hidden,
                       d.activation_token, d.activation_used_at
                FROM fpoc.drivers d
                LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id
                {where}
                ORDER BY d.empresa_id, d.vehicle_id""",
            *params,
        )
        return [_driver_row(r) for r in cur.fetchall()]


@router.post("/drivers", response_model=DriverOut)
def create_driver(req: DriverIn, user: CurrentUser = Depends(require_fleet_access)) -> DriverOut:
    enforce_fleet_empresa(user, req.empresa_id)
    # CR-014: token de activación para evitar el bloqueo 63112 de Meta.
    activation_token = gen_activation_token()
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.drivers
                    (driver_id, name, phone, license, empresa_id, vehicle_id, vehicle_name,
                     rating, deliveries_30d, fail_rate_30d, joined_at, active,
                     activation_token)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                req.driver_id, req.name, req.phone, req.license,
                req.empresa_id, req.vehicle_id, req.vehicle_name,
                req.rating, req.deliveries_30d, req.fail_rate_30d,
                req.joined_at.isoformat() if req.joined_at else None,
                1 if req.active else 0,
                activation_token,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"driver_id duplicado o datos inválidos: {e}")
    refresh_state_maestros()
    return fetch_driver(req.driver_id)


@router.get("/drivers/{driver_id}/activation-link", response_model=ActivationLinkOut)
def get_driver_activation_link(
    driver_id: str,
    user: CurrentUser = Depends(require_fleet_access),
) -> ActivationLinkOut:
    """Devuelve (o regenera) el wa.me activation link del driver.

    Si el token ya está usado, se regenera limpiando used_at. transport_manager
    solo puede pedirlo para drivers de SU empresa (enforce vía existing.empresa_id).
    """
    existing = fetch_driver(driver_id)  # 404 si no existe
    enforce_fleet_empresa(user, existing.empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT activation_token, activation_used_at FROM fpoc.drivers WHERE driver_id = ?",
            driver_id,
        )
        r = cur.fetchone()
        token = getattr(r, "activation_token", None) if r else None
        used_at = getattr(r, "activation_used_at", None) if r else None
        if not token or used_at is not None:
            token = gen_activation_token()
            cur.execute(
                "UPDATE fpoc.drivers SET activation_token = ?, activation_used_at = NULL "
                "WHERE driver_id = ?",
                token, driver_id,
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


@router.put("/drivers/{driver_id}", response_model=DriverOut)
def update_driver(driver_id: str, req: DriverUpdate,
                  user: CurrentUser = Depends(require_fleet_access)) -> DriverOut:
    existing = fetch_driver(driver_id)
    enforce_fleet_empresa(user, existing.empresa_id)
    if req.empresa_id is not None:
        enforce_fleet_empresa(user, req.empresa_id)
    sets, params = [], []
    for field in ["name", "phone", "license", "empresa_id", "vehicle_id", "vehicle_name",
                  "rating", "deliveries_30d", "fail_rate_30d"]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{field} = ?"); params.append(v)
    if req.joined_at is not None:
        sets.append("joined_at = ?"); params.append(req.joined_at.isoformat())
    if req.active is not None:
        sets.append("active = ?"); params.append(1 if req.active else 0)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.drivers SET {', '.join(sets)} WHERE driver_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "driver no encontrado")
        cn.commit()
    refresh_state_maestros()
    return fetch_driver(driver_id)


@router.delete("/drivers/{driver_id}")
def delete_driver(driver_id: str, user: CurrentUser = Depends(require_fleet_access)) -> dict:
    existing = fetch_driver(driver_id)
    enforce_fleet_empresa(user, existing.empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.drivers WHERE driver_id = ?", driver_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "driver no encontrado")
        cn.commit()
    refresh_state_maestros()
    return {"deleted": driver_id}

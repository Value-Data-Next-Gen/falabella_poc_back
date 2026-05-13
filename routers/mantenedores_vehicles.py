"""CRUD admin de vehículos. Extraído de mantenedores.py en R7-F4.

URLs (todas bajo /api/admin del router padre):
  GET    /api/admin/vehicles
  POST   /api/admin/vehicles
  PUT    /api/admin/vehicles/{vehicle_id}
  DELETE /api/admin/vehicles/{vehicle_id}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import CurrentUser
from core.db import get_conn
from routers.mantenedores_shared import (
    enforce_fleet_empresa,
    refresh_state_maestros,
    require_fleet_access,
)


router = APIRouter(tags=["admin-maestros"])


class VehicleIn(BaseModel):
    vehicle_id: int = Field(ge=1)
    empresa_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=50)
    type: str = Field(min_length=1, max_length=50)
    plate: str = Field(min_length=1, max_length=20)
    capacity_m3: int = Field(ge=0)
    driver_id: Optional[str] = Field(default=None, max_length=20)
    driver_name: Optional[str] = Field(default=None, max_length=200)
    depot_lat: float = -33.45
    depot_lon: float = -70.66
    year: Optional[int] = Field(default=None, ge=1990, le=2100)
    active: bool = True


class VehicleUpdate(BaseModel):
    empresa_id: Optional[int] = Field(default=None, ge=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    type: Optional[str] = Field(default=None, min_length=1, max_length=50)
    plate: Optional[str] = Field(default=None, min_length=1, max_length=20)
    capacity_m3: Optional[int] = Field(default=None, ge=0)
    driver_id: Optional[str] = Field(default=None, max_length=20)
    driver_name: Optional[str] = Field(default=None, max_length=200)
    year: Optional[int] = Field(default=None, ge=1990, le=2100)
    active: Optional[bool] = None


class VehicleOut(BaseModel):
    vehicle_id: int
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    name: str
    type: str
    plate: str
    capacity_m3: int
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    depot_lat: float
    depot_lon: float
    year: Optional[int] = None
    active: bool
    is_problem_hidden: bool = False


def _vehicle_row(r) -> VehicleOut:
    return VehicleOut(
        vehicle_id=int(r.vehicle_id),
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        empresa_nombre=getattr(r, "empresa_nombre", None),
        name=r.name, type=r.type, plate=r.plate,
        capacity_m3=int(r.capacity_m3),
        driver_id=r.driver_id, driver_name=r.driver_name,
        depot_lat=float(r.depot_lat), depot_lon=float(r.depot_lon),
        year=int(r.year) if r.year is not None else None,
        active=bool(r.active),
        is_problem_hidden=bool(r.is_problem_hidden),
    )


def fetch_vehicle(vehicle_id: int) -> VehicleOut:
    """Lee un vehicle por id. Helper público para otros sub-módulos."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT v.vehicle_id, v.empresa_id, e.nombre AS empresa_nombre,
                       v.name, v.type, v.plate, v.capacity_m3, v.driver_id, v.driver_name,
                       v.depot_lat, v.depot_lon, v.year, v.active, v.is_problem_hidden
                FROM fpoc.vehicles v
                LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = v.empresa_id
                WHERE v.vehicle_id = ?""",
            vehicle_id,
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "vehicle no encontrado")
        return _vehicle_row(r)


@router.get("/vehicles", response_model=list[VehicleOut])
def list_vehicles(user: CurrentUser = Depends(require_fleet_access)) -> list[VehicleOut]:
    where = "" if user.is_falabella else "WHERE v.empresa_id = ?"
    params: list = [] if user.is_falabella else [user.empresa_id]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT v.vehicle_id, v.empresa_id, e.nombre AS empresa_nombre,
                       v.name, v.type, v.plate, v.capacity_m3, v.driver_id, v.driver_name,
                       v.depot_lat, v.depot_lon, v.year, v.active, v.is_problem_hidden
                FROM fpoc.vehicles v
                LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = v.empresa_id
                {where}
                ORDER BY v.empresa_id, v.vehicle_id""",
            *params,
        )
        return [_vehicle_row(r) for r in cur.fetchall()]


@router.post("/vehicles", response_model=VehicleOut)
def create_vehicle(req: VehicleIn, user: CurrentUser = Depends(require_fleet_access)) -> VehicleOut:
    enforce_fleet_empresa(user, req.empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.vehicles
                    (vehicle_id, empresa_id, name, type, plate, capacity_m3, driver_id, driver_name,
                     depot_lat, depot_lon, year, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                req.vehicle_id, req.empresa_id, req.name, req.type, req.plate, req.capacity_m3,
                req.driver_id, req.driver_name, req.depot_lat, req.depot_lon,
                req.year, 1 if req.active else 0,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"vehicle_id duplicado o datos inválidos: {e}")
    refresh_state_maestros()
    return fetch_vehicle(req.vehicle_id)


@router.put("/vehicles/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(vehicle_id: int, req: VehicleUpdate,
                   user: CurrentUser = Depends(require_fleet_access)) -> VehicleOut:
    existing = fetch_vehicle(vehicle_id)
    enforce_fleet_empresa(user, existing.empresa_id)
    if req.empresa_id is not None:
        enforce_fleet_empresa(user, req.empresa_id)
    sets, params = [], []
    for field in ["empresa_id", "name", "type", "plate", "capacity_m3", "driver_id", "driver_name", "year"]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{field} = ?"); params.append(v)
    if req.active is not None:
        sets.append("active = ?"); params.append(1 if req.active else 0)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(vehicle_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.vehicles SET {', '.join(sets)} WHERE vehicle_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "vehicle no encontrado")
        cn.commit()
    refresh_state_maestros()
    return fetch_vehicle(vehicle_id)


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, user: CurrentUser = Depends(require_fleet_access)) -> dict:
    existing = fetch_vehicle(vehicle_id)
    enforce_fleet_empresa(user, existing.empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.vehicles WHERE vehicle_id = ?", vehicle_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "vehicle no encontrado")
        cn.commit()
    refresh_state_maestros()
    return {"deleted": vehicle_id}

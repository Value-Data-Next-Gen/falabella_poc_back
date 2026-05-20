"""Dotación diaria — quién opera cada vehículo cada día.

Extraído de mantenedores.py en R7-F4.

URLs (todas bajo /api/admin del router padre):
  GET    /api/admin/dotacion-diaria?fecha=&empresa_id=
  PUT    /api/admin/dotacion-diaria   (upsert por (fecha, empresa, driver|vehicle))

`fpoc.dotacion_diaria` guarda solo overrides — los drivers/vehículos activos
sin override quedan implícitamente en estado 'disponible'.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import get_conn
from routers.mantenedores_shared import can_access_empresa


router = APIRouter(tags=["admin-maestros"])


DotacionEstado = Literal["disponible", "ausente", "licencia", "mantencion", "baja", "reemplazo"]


class DotacionUpdate(BaseModel):
    fecha: date
    empresa_id: int = Field(ge=1)
    driver_id: Optional[str] = Field(default=None, max_length=20)
    vehicle_id: Optional[int] = Field(default=None, ge=1)
    estado: DotacionEstado = "disponible"
    motivo: Optional[str] = Field(default=None, max_length=500)


class DotacionRowOut(BaseModel):
    fecha: str
    empresa_id: int
    empresa_nombre: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_active: bool = True
    default_vehicle_id: Optional[int] = None
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    plate: Optional[str] = None
    vehicle_active: bool = True
    estado: DotacionEstado = "disponible"
    motivo: Optional[str] = None
    updated_at: Optional[str] = None


def _dotacion_empresa_ids(user: CurrentUser, empresa_id: Optional[int]) -> list[int]:
    if not user.is_falabella:
        if user.empresa_id is None:
            return []
        return [int(user.empresa_id)]
    if empresa_id is not None:
        return [int(empresa_id)]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT empresa_id FROM fpoc.empresas_transporte WHERE activo = 1 ORDER BY empresa_id")
        return [int(r.empresa_id) for r in cur.fetchall()]


def _fetch_dotacion_rows(fecha: date, empresa_ids: list[int]) -> list[DotacionRowOut]:
    if not empresa_ids:
        return []
    marks = ",".join(["?"] * len(empresa_ids))
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT empresa_id, nombre
            FROM fpoc.empresas_transporte
            WHERE empresa_id IN ({marks})
            """,
            *empresa_ids,
        )
        empresas = {int(r.empresa_id): r.nombre for r in cur.fetchall()}

        cur.execute(
            f"""
            SELECT d.driver_id, d.name, d.empresa_id, d.vehicle_id, d.vehicle_name, d.active
            FROM fpoc.drivers d
            WHERE d.empresa_id IN ({marks}) AND d.active = 1
            ORDER BY d.empresa_id, d.vehicle_id, d.name
            """,
            *empresa_ids,
        )
        drivers = cur.fetchall()

        cur.execute(
            f"""
            SELECT v.vehicle_id, v.empresa_id, v.name, v.plate, v.driver_id, v.driver_name, v.active
            FROM fpoc.vehicles v
            WHERE v.empresa_id IN ({marks}) AND v.active = 1
            ORDER BY v.empresa_id, v.vehicle_id
            """,
            *empresa_ids,
        )
        vehicles = cur.fetchall()
        vehicle_by_id = {int(v.vehicle_id): v for v in vehicles}

        cur.execute(
            f"""
            SELECT dotacion_id, fecha, empresa_id, driver_id, vehicle_id, estado, motivo, updated_at
            FROM fpoc.dotacion_diaria
            WHERE fecha = ? AND empresa_id IN ({marks})
            """,
            fecha.isoformat(), *empresa_ids,
        )
        overrides = cur.fetchall()

    by_driver = {str(r.driver_id): r for r in overrides if r.driver_id is not None}
    by_vehicle = {int(r.vehicle_id): r for r in overrides if r.vehicle_id is not None}
    used_vehicle_ids: set[int] = set()
    out: list[DotacionRowOut] = []

    for d in drivers:
        default_vid = int(d.vehicle_id) if d.vehicle_id is not None else None
        ov = by_driver.get(str(d.driver_id))
        if ov is None and default_vid is not None:
            ov = by_vehicle.get(default_vid)
        vid = int(ov.vehicle_id) if ov is not None and ov.vehicle_id is not None else default_vid
        vehicle = vehicle_by_id.get(vid) if vid is not None else None
        if vid is not None:
            used_vehicle_ids.add(vid)
        estado = str(ov.estado) if ov is not None else "disponible"
        out.append(DotacionRowOut(
            fecha=fecha.isoformat(),
            empresa_id=int(d.empresa_id),
            empresa_nombre=empresas.get(int(d.empresa_id)),
            driver_id=str(d.driver_id),
            driver_name=str(d.name),
            driver_active=bool(d.active),
            default_vehicle_id=default_vid,
            vehicle_id=vid,
            vehicle_name=(str(vehicle.name) if vehicle is not None else d.vehicle_name),
            plate=(str(vehicle.plate) if vehicle is not None and vehicle.plate is not None else None),
            vehicle_active=bool(vehicle.active) if vehicle is not None else True,
            estado=estado,  # type: ignore[arg-type]
            motivo=str(ov.motivo) if ov is not None and ov.motivo else None,
            updated_at=(
                ov.updated_at.isoformat()
                if ov is not None and hasattr(ov.updated_at, "isoformat")
                else (str(ov.updated_at) if ov is not None and ov.updated_at is not None else None)
            ),
        ))

    for v in vehicles:
        vid = int(v.vehicle_id)
        if vid in used_vehicle_ids:
            continue
        ov = by_vehicle.get(vid)
        estado = str(ov.estado) if ov is not None else "disponible"
        out.append(DotacionRowOut(
            fecha=fecha.isoformat(),
            empresa_id=int(v.empresa_id),
            empresa_nombre=empresas.get(int(v.empresa_id)),
            driver_id=None,
            driver_name=None,
            driver_active=True,
            default_vehicle_id=vid,
            vehicle_id=vid,
            vehicle_name=str(v.name),
            plate=str(v.plate) if v.plate is not None else None,
            vehicle_active=bool(v.active),
            estado=estado,  # type: ignore[arg-type]
            motivo=str(ov.motivo) if ov is not None and ov.motivo else None,
            updated_at=(
                ov.updated_at.isoformat()
                if ov is not None and hasattr(ov.updated_at, "isoformat")
                else (str(ov.updated_at) if ov is not None and ov.updated_at is not None else None)
            ),
        ))

    out.sort(key=lambda r: (r.empresa_id, r.vehicle_id or 0, r.driver_name or ""))
    return out


def _validate_dotacion_target(cn, req: DotacionUpdate) -> None:
    if req.driver_id is None and req.vehicle_id is None:
        raise HTTPException(400, "driver_id o vehicle_id requerido")
    cur = cn.cursor()
    if req.driver_id is not None:
        cur.execute("SELECT empresa_id FROM fpoc.drivers WHERE driver_id = ?", req.driver_id)
        d = cur.fetchone()
        if not d:
            raise HTTPException(404, "driver no encontrado")
        if int(d.empresa_id) != req.empresa_id:
            raise HTTPException(400, "driver no pertenece a la empresa indicada")
    if req.vehicle_id is not None:
        cur.execute("SELECT empresa_id FROM fpoc.vehicles WHERE vehicle_id = ?", req.vehicle_id)
        v = cur.fetchone()
        if not v:
            raise HTTPException(404, "vehicle no encontrado")
        if int(v.empresa_id) != req.empresa_id:
            raise HTTPException(400, "vehiculo no pertenece a la empresa indicada")


@router.get("/dotacion-diaria", response_model=list[DotacionRowOut])
def list_dotacion_diaria(
    fecha: Optional[date] = Query(default=None),
    empresa_id: Optional[int] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> list[DotacionRowOut]:
    fecha = fecha or date.today()
    empresa_ids = _dotacion_empresa_ids(user, empresa_id)
    for eid in empresa_ids:
        can_access_empresa(user, eid)
    return _fetch_dotacion_rows(fecha, empresa_ids)


@router.put("/dotacion-diaria")
def upsert_dotacion_diaria(
    req: DotacionUpdate,
    user: CurrentUser = Depends(current_user),
) -> dict:
    can_access_empresa(user, req.empresa_id)
    with get_conn() as cn:
        _validate_dotacion_target(cn, req)
        cur = cn.cursor()
        if req.driver_id is not None:
            cur.execute(
                """
                SELECT dotacion_id
                FROM fpoc.dotacion_diaria
                WHERE fecha = ? AND empresa_id = ? AND driver_id = ?
                """,
                req.fecha.isoformat(), req.empresa_id, req.driver_id,
            )
        else:
            cur.execute(
                """
                SELECT dotacion_id
                FROM fpoc.dotacion_diaria
                WHERE fecha = ? AND empresa_id = ? AND driver_id IS NULL AND vehicle_id = ?
                """,
                req.fecha.isoformat(), req.empresa_id, req.vehicle_id,
            )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE fpoc.dotacion_diaria
                   SET vehicle_id = ?, estado = ?, motivo = ?,
                       updated_by_user_id = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE dotacion_id = ?
                """,
                req.vehicle_id, req.estado, req.motivo, user.user_id, int(existing.dotacion_id),
            )
            dotacion_id = int(existing.dotacion_id)
        else:
            cur.execute(
                """
                INSERT INTO fpoc.dotacion_diaria
                    (fecha, empresa_id, driver_id, vehicle_id, estado, motivo, updated_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                req.fecha.isoformat(), req.empresa_id, req.driver_id, req.vehicle_id,
                req.estado, req.motivo, user.user_id,
            )
            dotacion_id = int(getattr(cur, "lastrowid", 0) or 0)
        cn.commit()
    return {"status": "ok", "dotacion_id": dotacion_id}

"""Capacitaciones — catálogo de módulos + registros por driver.

Extraído de mantenedores.py en R7-F4.

URLs (todas bajo /api/admin del router padre):
  Catálogo de módulos:
    GET    /capacitacion-modulos?only_active=
    POST   /capacitacion-modulos
    PUT    /capacitacion-modulos/{modulo_id}
    DELETE /capacitacion-modulos/{modulo_id}

  Registros por driver:
    GET    /drivers/{driver_id}/capacitaciones
    POST   /drivers/{driver_id}/capacitaciones
    PUT    /drivers/{driver_id}/capacitaciones/{cap_id}
    DELETE /drivers/{driver_id}/capacitaciones/{cap_id}
    POST   /drivers/{driver_id}/capacitaciones/{cap_id}/validate
    POST   /drivers/{driver_id}/capacitaciones/{cap_id}/unvalidate

Validación: solo Falabella admin/ops puede validar/des-validar
(transport_manager carga el comprobante, Falabella confirma).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user, require_admin
from core.db import get_conn
from routers.mantenedores_documents_driver import enforce_driver_access
from routers.mantenedores_shared import require_fleet_access


router = APIRouter(tags=["admin-maestros"])


# =============================================================================
# Catálogo de módulos
# =============================================================================
class CapacitacionModuloIn(BaseModel):
    codigo: str = Field(min_length=1, max_length=50)
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: Optional[str] = Field(default=None, max_length=500)
    validez_meses: int = Field(ge=1, le=120, default=12)
    activo: bool = True


class CapacitacionModuloUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=200)
    descripcion: Optional[str] = Field(default=None, max_length=500)
    validez_meses: Optional[int] = Field(default=None, ge=1, le=120)
    activo: Optional[bool] = None


class CapacitacionModuloOut(BaseModel):
    modulo_id: int
    codigo: str
    nombre: str
    descripcion: Optional[str] = None
    validez_meses: int
    activo: bool


def _modulo_row(r) -> CapacitacionModuloOut:
    return CapacitacionModuloOut(
        modulo_id=int(r.modulo_id),
        codigo=str(r.codigo),
        nombre=str(r.nombre),
        descripcion=r.descripcion,
        validez_meses=int(r.validez_meses),
        activo=bool(r.activo),
    )


@router.get("/capacitacion-modulos", response_model=list[CapacitacionModuloOut])
def list_capacitacion_modulos(
    only_active: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> list[CapacitacionModuloOut]:
    where = "WHERE activo = 1" if only_active else ""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT modulo_id, codigo, nombre, descripcion, validez_meses, activo
                FROM fpoc.capacitacion_modulos
                {where}
                ORDER BY nombre"""
        )
        return [_modulo_row(r) for r in cur.fetchall()]


@router.post("/capacitacion-modulos", response_model=CapacitacionModuloOut)
def create_capacitacion_modulo(
    req: CapacitacionModuloIn,
    _: CurrentUser = Depends(require_admin),
) -> CapacitacionModuloOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.capacitacion_modulos
                    (codigo, nombre, descripcion, validez_meses, activo)
                   VALUES (?, ?, ?, ?, ?)""",
                req.codigo, req.nombre, req.descripcion, req.validez_meses,
                1 if req.activo else 0,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"código duplicado o datos inválidos: {e}")
        cur.execute(
            """SELECT modulo_id, codigo, nombre, descripcion, validez_meses, activo
                FROM fpoc.capacitacion_modulos WHERE codigo = ?""",
            req.codigo,
        )
        return _modulo_row(cur.fetchone())


@router.put("/capacitacion-modulos/{modulo_id}", response_model=CapacitacionModuloOut)
def update_capacitacion_modulo(
    modulo_id: int,
    req: CapacitacionModuloUpdate,
    _: CurrentUser = Depends(require_admin),
) -> CapacitacionModuloOut:
    sets, params = [], []
    if req.nombre is not None:
        sets.append("nombre = ?"); params.append(req.nombre)
    if req.descripcion is not None:
        sets.append("descripcion = ?"); params.append(req.descripcion)
    if req.validez_meses is not None:
        sets.append("validez_meses = ?"); params.append(req.validez_meses)
    if req.activo is not None:
        sets.append("activo = ?"); params.append(1 if req.activo else 0)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(modulo_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.capacitacion_modulos SET {', '.join(sets)} WHERE modulo_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "módulo no encontrado")
        cn.commit()
        cur.execute(
            """SELECT modulo_id, codigo, nombre, descripcion, validez_meses, activo
                FROM fpoc.capacitacion_modulos WHERE modulo_id = ?""",
            modulo_id,
        )
        return _modulo_row(cur.fetchone())


@router.delete("/capacitacion-modulos/{modulo_id}")
def delete_capacitacion_modulo(
    modulo_id: int,
    _: CurrentUser = Depends(require_admin),
) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.capacitacion_modulos WHERE modulo_id = ?", modulo_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "módulo no encontrado")
        cn.commit()
    return {"deleted": modulo_id}


# =============================================================================
# Registros por driver
# =============================================================================
class DriverCapacitacionIn(BaseModel):
    modulo_id: int = Field(ge=1)
    fecha_completado: date
    vence_at: Optional[date] = None  # si null, calculamos = fecha + validez_meses
    notas: Optional[str] = Field(default=None, max_length=500)
    doc_id: Optional[int] = None


class DriverCapacitacionUpdate(BaseModel):
    fecha_completado: Optional[date] = None
    vence_at: Optional[date] = None
    notas: Optional[str] = Field(default=None, max_length=500)
    doc_id: Optional[int] = None


class DriverCapacitacionOut(BaseModel):
    cap_id: int
    driver_id: str
    modulo_id: int
    modulo_codigo: str
    modulo_nombre: str
    fecha_completado: str
    vence_at: Optional[str] = None
    notas: Optional[str] = None
    doc_id: Optional[int] = None
    created_by: Optional[int] = None
    created_at: str
    validated_by_user_id: Optional[int] = None
    validated_at: Optional[str] = None
    validated_by_name: Optional[str] = None


def _cap_row(r) -> DriverCapacitacionOut:
    def _iso(v):
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
    return DriverCapacitacionOut(
        cap_id=int(r.cap_id),
        driver_id=str(r.driver_id),
        modulo_id=int(r.modulo_id),
        modulo_codigo=str(r.modulo_codigo),
        modulo_nombre=str(r.modulo_nombre),
        fecha_completado=_iso(r.fecha_completado) or "",
        vence_at=_iso(r.vence_at),
        notas=r.notas,
        doc_id=int(r.doc_id) if r.doc_id is not None else None,
        created_by=int(r.created_by) if r.created_by is not None else None,
        created_at=_iso(r.created_at) or "",
        validated_by_user_id=int(r.validated_by_user_id) if getattr(r, "validated_by_user_id", None) is not None else None,
        validated_at=_iso(getattr(r, "validated_at", None)),
        validated_by_name=getattr(r, "validated_by_name", None),
    )


def _compute_vence_at(fecha_completado: date, validez_meses: int) -> date:
    """Suma N meses a la fecha (manejando fin de mes)."""
    from calendar import monthrange
    y = fecha_completado.year
    m = fecha_completado.month + validez_meses
    while m > 12:
        m -= 12
        y += 1
    last_day = monthrange(y, m)[1]
    d = min(fecha_completado.day, last_day)
    return date(y, m, d)


_CAP_SELECT = """
    SELECT c.cap_id, c.driver_id, c.modulo_id,
           m.codigo AS modulo_codigo, m.nombre AS modulo_nombre,
           c.fecha_completado, c.vence_at, c.notas, c.doc_id,
           c.created_by, c.created_at,
           c.validated_by_user_id, c.validated_at,
           vu.display_name AS validated_by_name
    FROM fpoc.driver_capacitaciones c
    INNER JOIN fpoc.capacitacion_modulos m ON m.modulo_id = c.modulo_id
    LEFT JOIN fpoc.users vu ON vu.user_id = c.validated_by_user_id
"""


@router.get("/drivers/{driver_id}/capacitaciones", response_model=list[DriverCapacitacionOut])
def list_driver_capacitaciones(
    driver_id: str,
    user: CurrentUser = Depends(require_fleet_access),
) -> list[DriverCapacitacionOut]:
    enforce_driver_access(user, driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            _CAP_SELECT + " WHERE c.driver_id = ? ORDER BY c.fecha_completado DESC",
            driver_id,
        )
        return [_cap_row(r) for r in cur.fetchall()]


@router.post("/drivers/{driver_id}/capacitaciones", response_model=DriverCapacitacionOut)
def create_driver_capacitacion(
    driver_id: str,
    req: DriverCapacitacionIn,
    user: CurrentUser = Depends(require_fleet_access),
) -> DriverCapacitacionOut:
    enforce_driver_access(user, driver_id)
    vence_at = req.vence_at
    if vence_at is None:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT validez_meses FROM fpoc.capacitacion_modulos WHERE modulo_id = ?",
                req.modulo_id,
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(400, "módulo inválido")
            vence_at = _compute_vence_at(req.fecha_completado, int(row.validez_meses))

    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.driver_capacitaciones
                    (driver_id, modulo_id, fecha_completado, vence_at, notas, doc_id, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                driver_id, req.modulo_id,
                req.fecha_completado.isoformat(), vence_at.isoformat() if vence_at else None,
                req.notas, req.doc_id, user.user_id,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(400, f"error guardando capacitación: {e}")
        cur.execute(
            "SELECT TOP 1" + _CAP_SELECT.split("SELECT", 1)[1] +
            " WHERE c.driver_id = ? AND c.modulo_id = ? ORDER BY c.cap_id DESC",
            driver_id, req.modulo_id,
        )
        return _cap_row(cur.fetchone())


@router.put("/drivers/{driver_id}/capacitaciones/{cap_id}", response_model=DriverCapacitacionOut)
def update_driver_capacitacion(
    driver_id: str,
    cap_id: int,
    req: DriverCapacitacionUpdate,
    user: CurrentUser = Depends(require_fleet_access),
) -> DriverCapacitacionOut:
    enforce_driver_access(user, driver_id)
    sets, params = [], []
    if req.fecha_completado is not None:
        sets.append("fecha_completado = ?"); params.append(req.fecha_completado.isoformat())
    if req.vence_at is not None:
        sets.append("vence_at = ?"); params.append(req.vence_at.isoformat())
    if req.notas is not None:
        sets.append("notas = ?"); params.append(req.notas)
    if req.doc_id is not None:
        sets.append("doc_id = ?"); params.append(req.doc_id)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params += [cap_id, driver_id]
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE fpoc.driver_capacitaciones SET {', '.join(sets)} "
            f"WHERE cap_id = ? AND driver_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "capacitación no encontrada")
        cn.commit()
        cur.execute(_CAP_SELECT + " WHERE c.cap_id = ?", cap_id)
        return _cap_row(cur.fetchone())


@router.delete("/drivers/{driver_id}/capacitaciones/{cap_id}")
def delete_driver_capacitacion(
    driver_id: str,
    cap_id: int,
    user: CurrentUser = Depends(require_fleet_access),
) -> dict:
    enforce_driver_access(user, driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "DELETE FROM fpoc.driver_capacitaciones WHERE cap_id = ? AND driver_id = ?",
            cap_id, driver_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "capacitación no encontrada")
        cn.commit()
    return {"deleted": cap_id}


@router.post("/drivers/{driver_id}/capacitaciones/{cap_id}/validate",
             response_model=DriverCapacitacionOut)
def validate_driver_capacitacion(
    driver_id: str,
    cap_id: int,
    user: CurrentUser = Depends(current_user),
) -> DriverCapacitacionOut:
    """Marca la capacitación como VALIDADA por Falabella.
    Solo admin/ops puede validar (manager carga el comprobante, Falabella confirma)."""
    if not user.is_falabella:
        raise HTTPException(403, "Solo Falabella (admin/ops) puede validar capacitaciones")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """UPDATE fpoc.driver_capacitaciones
                SET validated_by_user_id = ?, validated_at = CURRENT_TIMESTAMP
              WHERE cap_id = ? AND driver_id = ?""",
            user.user_id, cap_id, driver_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "capacitación no encontrada")
        cn.commit()
        cur.execute(_CAP_SELECT + " WHERE c.cap_id = ?", cap_id)
        return _cap_row(cur.fetchone())


@router.post("/drivers/{driver_id}/capacitaciones/{cap_id}/unvalidate",
             response_model=DriverCapacitacionOut)
def unvalidate_driver_capacitacion(
    driver_id: str,
    cap_id: int,
    user: CurrentUser = Depends(current_user),
) -> DriverCapacitacionOut:
    if not user.is_falabella:
        raise HTTPException(403, "Solo Falabella puede des-validar")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """UPDATE fpoc.driver_capacitaciones
                SET validated_by_user_id = NULL, validated_at = NULL
              WHERE cap_id = ? AND driver_id = ?""",
            cap_id, driver_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "capacitación no encontrada")
        cn.commit()
        cur.execute(_CAP_SELECT + " WHERE c.cap_id = ?", cap_id)
        return _cap_row(cur.fetchone())

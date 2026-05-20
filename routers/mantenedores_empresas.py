"""CRUD admin de empresas transportistas. Extraído de mantenedores.py en R7-F4.

El router NO tiene prefix propio — se incluye en `mantenedores.router` (que
sí lo tiene: /api/admin) vía `include_router`. URLs públicas resultantes:

  GET    /api/admin/empresas
  POST   /api/admin/empresas
  PUT    /api/admin/empresas/{empresa_id}
  DELETE /api/admin/empresas/{empresa_id}

Todos requieren rol falabella_admin.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import CurrentUser, require_admin
from core.db import get_conn


router = APIRouter(tags=["admin-maestros"])


class EmpresaIn(BaseModel):
    empresa_id: int = Field(ge=1)
    nombre: str = Field(min_length=1, max_length=100)
    activo: bool = True
    central_phone: Optional[str] = Field(default=None, max_length=20)


class EmpresaUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=100)
    activo: Optional[bool] = None
    central_phone: Optional[str] = Field(default=None, max_length=20)


class EmpresaOut(BaseModel):
    empresa_id: int
    nombre: str
    activo: bool
    central_phone: Optional[str] = None
    created_at: Optional[str] = None


def _empresa_row(r) -> EmpresaOut:
    created = r.created_at
    return EmpresaOut(
        empresa_id=int(r.empresa_id),
        nombre=r.nombre,
        activo=bool(r.activo),
        central_phone=getattr(r, "central_phone", None),
        created_at=created.isoformat() if hasattr(created, "isoformat") else (created or None),
    )


_EMPRESA_COLS = "empresa_id, nombre, activo, central_phone, created_at"


@router.get("/empresas", response_model=list[EmpresaOut])
def list_empresas(_: CurrentUser = Depends(require_admin)) -> list[EmpresaOut]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT {_EMPRESA_COLS} FROM fpoc.empresas_transporte ORDER BY empresa_id"
        )
        return [_empresa_row(r) for r in cur.fetchall()]


@router.post("/empresas", response_model=EmpresaOut)
def create_empresa(req: EmpresaIn, _: CurrentUser = Depends(require_admin)) -> EmpresaOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                "INSERT INTO fpoc.empresas_transporte (empresa_id, nombre, activo, central_phone) "
                "VALUES (?, ?, ?, ?)",
                req.empresa_id, req.nombre, 1 if req.activo else 0, req.central_phone,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"empresa duplicada o inválida: {e}")
        cur.execute(
            f"SELECT {_EMPRESA_COLS} FROM fpoc.empresas_transporte WHERE empresa_id = ?",
            req.empresa_id,
        )
        return _empresa_row(cur.fetchone())


@router.put("/empresas/{empresa_id}", response_model=EmpresaOut)
def update_empresa(empresa_id: int, req: EmpresaUpdate,
                   _: CurrentUser = Depends(require_admin)) -> EmpresaOut:
    sets, params = [], []
    if req.nombre is not None:
        sets.append("nombre = ?"); params.append(req.nombre)
    if req.activo is not None:
        sets.append("activo = ?"); params.append(1 if req.activo else 0)
    if req.central_phone is not None:
        sets.append("central_phone = ?"); params.append(req.central_phone or None)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE fpoc.empresas_transporte SET {', '.join(sets)} WHERE empresa_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "empresa no encontrada")
        cn.commit()
        cur.execute(
            f"SELECT {_EMPRESA_COLS} FROM fpoc.empresas_transporte WHERE empresa_id = ?",
            empresa_id,
        )
        return _empresa_row(cur.fetchone())


@router.delete("/empresas/{empresa_id}")
def delete_empresa(empresa_id: int, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT COUNT(*) FROM fpoc.users WHERE empresa_id = ?", empresa_id)
        n_users = int(cur.fetchone()[0])
        if n_users > 0:
            raise HTTPException(409, f"empresa tiene {n_users} usuarios; desactivar en vez de eliminar")
        cur.execute("DELETE FROM fpoc.empresas_transporte WHERE empresa_id = ?", empresa_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "empresa no encontrada")
        cn.commit()
    return {"deleted": empresa_id}

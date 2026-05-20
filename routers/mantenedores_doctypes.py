"""Catálogo de tipos de documentos por entidad (driver/vehicle/empresa).

Extraído de mantenedores.py en R7-F4.

URLs (todas bajo /api/admin del router padre):
  GET    /api/admin/document-types?entity_type=&only_active=
  POST   /api/admin/document-types
  PUT    /api/admin/document-types/{doc_type_id}
  DELETE /api/admin/document-types/{doc_type_id}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user, require_admin
from core.db import get_conn


router = APIRouter(tags=["admin-maestros"])


class DocTypeIn(BaseModel):
    entity_type: str = Field(pattern="^(driver|vehicle|empresa)$")
    codigo: str = Field(min_length=1, max_length=50)
    nombre: str = Field(min_length=1, max_length=200)
    mandatory: bool = False
    validez_meses: Optional[int] = Field(default=None, ge=1, le=600)
    active: bool = True


class DocTypeUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=200)
    mandatory: Optional[bool] = None
    validez_meses: Optional[int] = Field(default=None, ge=1, le=600)
    active: Optional[bool] = None


class DocTypeOut(BaseModel):
    doc_type_id: int
    entity_type: str
    codigo: str
    nombre: str
    mandatory: bool
    validez_meses: Optional[int] = None
    active: bool


def _doc_type_row(r) -> DocTypeOut:
    return DocTypeOut(
        doc_type_id=int(r.doc_type_id),
        entity_type=str(r.entity_type),
        codigo=str(r.codigo),
        nombre=str(r.nombre),
        mandatory=bool(r.mandatory),
        validez_meses=int(r.validez_meses) if r.validez_meses is not None else None,
        active=bool(r.active),
    )


@router.get("/document-types", response_model=list[DocTypeOut])
def list_document_types(
    entity_type: Optional[str] = Query(default=None),
    only_active: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> list[DocTypeOut]:
    where, params = [], []
    if entity_type:
        where.append("entity_type = ?"); params.append(entity_type)
    if only_active:
        where.append("active = 1")
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT doc_type_id, entity_type, codigo, nombre, mandatory, validez_meses, active
                FROM fpoc.document_types
                {where_sql}
                ORDER BY entity_type, nombre""",
            *params,
        )
        return [_doc_type_row(r) for r in cur.fetchall()]


@router.post("/document-types", response_model=DocTypeOut)
def create_document_type(req: DocTypeIn, _: CurrentUser = Depends(require_admin)) -> DocTypeOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.document_types
                    (entity_type, codigo, nombre, mandatory, validez_meses, active)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                req.entity_type, req.codigo, req.nombre,
                1 if req.mandatory else 0, req.validez_meses, 1 if req.active else 0,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"código duplicado para esa entidad o datos inválidos: {e}")
        cur.execute(
            """SELECT doc_type_id, entity_type, codigo, nombre, mandatory, validez_meses, active
                FROM fpoc.document_types WHERE entity_type = ? AND codigo = ?""",
            req.entity_type, req.codigo,
        )
        return _doc_type_row(cur.fetchone())


@router.put("/document-types/{doc_type_id}", response_model=DocTypeOut)
def update_document_type(doc_type_id: int, req: DocTypeUpdate,
                         _: CurrentUser = Depends(require_admin)) -> DocTypeOut:
    sets, params = [], []
    if req.nombre is not None:
        sets.append("nombre = ?"); params.append(req.nombre)
    if req.mandatory is not None:
        sets.append("mandatory = ?"); params.append(1 if req.mandatory else 0)
    if req.validez_meses is not None:
        sets.append("validez_meses = ?"); params.append(req.validez_meses)
    if req.active is not None:
        sets.append("active = ?"); params.append(1 if req.active else 0)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(doc_type_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.document_types SET {', '.join(sets)} WHERE doc_type_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "tipo no encontrado")
        cn.commit()
        cur.execute(
            """SELECT doc_type_id, entity_type, codigo, nombre, mandatory, validez_meses, active
                FROM fpoc.document_types WHERE doc_type_id = ?""",
            doc_type_id,
        )
        return _doc_type_row(cur.fetchone())


@router.delete("/document-types/{doc_type_id}")
def delete_document_type(doc_type_id: int, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.document_types WHERE doc_type_id = ?", doc_type_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "tipo no encontrado")
        cn.commit()
    return {"deleted": doc_type_id}

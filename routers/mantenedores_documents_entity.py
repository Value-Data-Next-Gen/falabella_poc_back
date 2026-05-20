"""Documentos de empresas y vehículos. Extraído en R7-F4.

Comparten modelo EntityDocOut y helpers porque la lógica es idéntica
(blob storage + INSERT/SELECT con id-column distinto).

URLs (bajo /api/admin del router padre):
  GET    /api/admin/empresas/{empresa_id}/documents
  POST   /api/admin/empresas/{empresa_id}/documents
  GET    /api/admin/empresas/{empresa_id}/documents/{doc_id}/download
  DELETE /api/admin/empresas/{empresa_id}/documents/{doc_id}

  GET    /api/admin/vehicles/{vehicle_id}/documents
  POST   /api/admin/vehicles/{vehicle_id}/documents
  GET    /api/admin/vehicles/{vehicle_id}/documents/{doc_id}/download
  DELETE /api/admin/vehicles/{vehicle_id}/documents/{doc_id}
"""
from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import CurrentUser
from core.db import get_conn
from routers.mantenedores_shared import enforce_fleet_empresa, require_fleet_access


router = APIRouter(tags=["admin-maestros"])


class EntityDocOut(BaseModel):
    doc_id: int
    entity_id: int  # empresa_id o vehicle_id
    tipo: str
    filename: str
    file_size: int
    content_type: Optional[str] = None
    uploaded_at: str
    uploaded_by_user_id: Optional[int] = None
    expires_at: Optional[str] = None
    notes: Optional[str] = None


def _entity_doc_row(r, id_col: str) -> EntityDocOut:
    return EntityDocOut(
        doc_id=int(r.doc_id),
        entity_id=int(getattr(r, id_col)),
        tipo=str(r.tipo),
        filename=str(r.filename),
        file_size=int(r.file_size or 0),
        content_type=r.content_type,
        uploaded_at=(r.uploaded_at.isoformat() if hasattr(r.uploaded_at, "isoformat") else str(r.uploaded_at)),
        uploaded_by_user_id=int(r.uploaded_by_user_id) if r.uploaded_by_user_id is not None else None,
        expires_at=(r.expires_at.isoformat() if hasattr(r.expires_at, "isoformat") else (str(r.expires_at) if r.expires_at else None)),
        notes=r.notes,
    )


def enforce_vehicle_access(user: CurrentUser, vehicle_id: int) -> None:
    """transport_manager solo puede tocar vehículos de su empresa."""
    if user.is_falabella:
        return
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT empresa_id FROM fpoc.vehicles WHERE vehicle_id = ?", vehicle_id)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "vehículo no encontrado")
        enforce_fleet_empresa(user, int(row.empresa_id) if row.empresa_id is not None else None)


# =============================================================================
# Empresa documents
# =============================================================================
@router.get("/empresas/{empresa_id}/documents", response_model=list[EntityDocOut])
def list_empresa_documents(empresa_id: int,
                           user: CurrentUser = Depends(require_fleet_access)) -> list[EntityDocOut]:
    enforce_fleet_empresa(user, empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT doc_id, empresa_id, tipo, filename, file_size, content_type,
                       uploaded_at, uploaded_by_user_id, expires_at, notes
                FROM fpoc.empresa_documents
                WHERE empresa_id = ? ORDER BY uploaded_at DESC""",
            empresa_id,
        )
        return [_entity_doc_row(r, "empresa_id") for r in cur.fetchall()]


@router.post("/empresas/{empresa_id}/documents", response_model=EntityDocOut)
async def upload_empresa_document(
    empresa_id: int,
    tipo: str = Query(...),
    expires_at: Optional[str] = Query(default=None),
    notes: Optional[str] = Query(default=None, max_length=500),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_fleet_access),
) -> EntityDocOut:
    enforce_fleet_empresa(user, empresa_id)
    data = await file.read()
    if not data:
        raise HTTPException(400, "archivo vacío")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "archivo > 25MB")
    import uuid as _uuid
    from core.storage import upload as storage_upload
    safe_name = (file.filename or "documento").replace("/", "_").replace("\\", "_")
    blob_path = f"empresas/{empresa_id}/{_uuid.uuid4().hex}_{safe_name}"
    storage_upload(blob_path, data, content_type=file.content_type)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """INSERT INTO fpoc.empresa_documents
                (empresa_id, tipo, filename, blob_path, file_size, content_type,
                 uploaded_by_user_id, expires_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            empresa_id, tipo, safe_name, blob_path, len(data),
            file.content_type, user.user_id, expires_at, notes,
        )
        cn.commit()
        cur.execute(
            """SELECT doc_id, empresa_id, tipo, filename, file_size, content_type,
                       uploaded_at, uploaded_by_user_id, expires_at, notes
                FROM fpoc.empresa_documents
                WHERE empresa_id = ? AND blob_path = ?""",
            empresa_id, blob_path,
        )
        return _entity_doc_row(cur.fetchone(), "empresa_id")


@router.get("/empresas/{empresa_id}/documents/{doc_id}/download")
def download_empresa_document(empresa_id: int, doc_id: int,
                              user: CurrentUser = Depends(require_fleet_access)) -> StreamingResponse:
    enforce_fleet_empresa(user, empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT filename, blob_path, content_type FROM fpoc.empresa_documents "
            "WHERE empresa_id = ? AND doc_id = ?",
            empresa_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
    from core.storage import download as storage_download
    try:
        data, ct = storage_download(str(row.blob_path))
    except FileNotFoundError:
        raise HTTPException(404, "blob ausente en storage")
    return StreamingResponse(
        io.BytesIO(data),
        media_type=row.content_type or ct or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row.filename}"'},
    )


@router.delete("/empresas/{empresa_id}/documents/{doc_id}")
def delete_empresa_document(empresa_id: int, doc_id: int,
                            user: CurrentUser = Depends(require_fleet_access)) -> dict:
    enforce_fleet_empresa(user, empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT blob_path FROM fpoc.empresa_documents WHERE empresa_id = ? AND doc_id = ?",
            empresa_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
        blob_path = str(row.blob_path)
        cur.execute("DELETE FROM fpoc.empresa_documents WHERE doc_id = ?", doc_id)
        cn.commit()
    from core.storage import delete as storage_delete
    storage_delete(blob_path)
    return {"deleted": doc_id}


# =============================================================================
# Vehicle documents
# =============================================================================
@router.get("/vehicles/{vehicle_id}/documents", response_model=list[EntityDocOut])
def list_vehicle_documents(vehicle_id: int,
                           user: CurrentUser = Depends(require_fleet_access)) -> list[EntityDocOut]:
    enforce_vehicle_access(user, vehicle_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT doc_id, vehicle_id, tipo, filename, file_size, content_type,
                       uploaded_at, uploaded_by_user_id, expires_at, notes
                FROM fpoc.vehicle_documents
                WHERE vehicle_id = ? ORDER BY uploaded_at DESC""",
            vehicle_id,
        )
        return [_entity_doc_row(r, "vehicle_id") for r in cur.fetchall()]


@router.post("/vehicles/{vehicle_id}/documents", response_model=EntityDocOut)
async def upload_vehicle_document(
    vehicle_id: int,
    tipo: str = Query(...),
    expires_at: Optional[str] = Query(default=None),
    notes: Optional[str] = Query(default=None, max_length=500),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_fleet_access),
) -> EntityDocOut:
    enforce_vehicle_access(user, vehicle_id)
    data = await file.read()
    if not data:
        raise HTTPException(400, "archivo vacío")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "archivo > 25MB")
    import uuid as _uuid
    from core.storage import upload as storage_upload
    safe_name = (file.filename or "documento").replace("/", "_").replace("\\", "_")
    blob_path = f"vehicles/{vehicle_id}/{_uuid.uuid4().hex}_{safe_name}"
    storage_upload(blob_path, data, content_type=file.content_type)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """INSERT INTO fpoc.vehicle_documents
                (vehicle_id, tipo, filename, blob_path, file_size, content_type,
                 uploaded_by_user_id, expires_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            vehicle_id, tipo, safe_name, blob_path, len(data),
            file.content_type, user.user_id, expires_at, notes,
        )
        cn.commit()
        cur.execute(
            """SELECT doc_id, vehicle_id, tipo, filename, file_size, content_type,
                       uploaded_at, uploaded_by_user_id, expires_at, notes
                FROM fpoc.vehicle_documents
                WHERE vehicle_id = ? AND blob_path = ?""",
            vehicle_id, blob_path,
        )
        return _entity_doc_row(cur.fetchone(), "vehicle_id")


@router.get("/vehicles/{vehicle_id}/documents/{doc_id}/download")
def download_vehicle_document(vehicle_id: int, doc_id: int,
                              user: CurrentUser = Depends(require_fleet_access)) -> StreamingResponse:
    enforce_vehicle_access(user, vehicle_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT filename, blob_path, content_type FROM fpoc.vehicle_documents "
            "WHERE vehicle_id = ? AND doc_id = ?",
            vehicle_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
    from core.storage import download as storage_download
    try:
        data, ct = storage_download(str(row.blob_path))
    except FileNotFoundError:
        raise HTTPException(404, "blob ausente en storage")
    return StreamingResponse(
        io.BytesIO(data),
        media_type=row.content_type or ct or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row.filename}"'},
    )


@router.delete("/vehicles/{vehicle_id}/documents/{doc_id}")
def delete_vehicle_document(vehicle_id: int, doc_id: int,
                            user: CurrentUser = Depends(require_fleet_access)) -> dict:
    enforce_vehicle_access(user, vehicle_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT blob_path FROM fpoc.vehicle_documents WHERE vehicle_id = ? AND doc_id = ?",
            vehicle_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
        blob_path = str(row.blob_path)
        cur.execute("DELETE FROM fpoc.vehicle_documents WHERE doc_id = ?", doc_id)
        cn.commit()
    from core.storage import delete as storage_delete
    storage_delete(blob_path)
    return {"deleted": doc_id}

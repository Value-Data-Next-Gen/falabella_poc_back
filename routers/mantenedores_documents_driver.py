"""Documentos de drivers. Extraído en R7-F4.

Schema en DB separado (fpoc.driver_documents) pero el patrón blob + INSERT/
SELECT es el mismo que entity_documents (empresas/vehicles).

URLs (bajo /api/admin del router padre):
  GET    /api/admin/drivers/{driver_id}/documents
  POST   /api/admin/drivers/{driver_id}/documents
  GET    /api/admin/drivers/{driver_id}/documents/{doc_id}/download
  DELETE /api/admin/drivers/{driver_id}/documents/{doc_id}
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


DOC_TIPOS = ("licencia", "antecedentes", "contrato", "poliza", "certificacion", "otro")


class DriverDocOut(BaseModel):
    doc_id: int
    driver_id: str
    tipo: str
    filename: str
    file_size: int
    content_type: Optional[str] = None
    uploaded_at: str
    uploaded_by_user_id: Optional[int] = None
    expires_at: Optional[str] = None
    notes: Optional[str] = None


def _doc_row(r) -> DriverDocOut:
    return DriverDocOut(
        doc_id=int(r.doc_id),
        driver_id=str(r.driver_id),
        tipo=str(r.tipo),
        filename=str(r.filename),
        file_size=int(r.file_size or 0),
        content_type=r.content_type,
        uploaded_at=(r.uploaded_at.isoformat() if hasattr(r.uploaded_at, "isoformat") else str(r.uploaded_at)),
        uploaded_by_user_id=int(r.uploaded_by_user_id) if r.uploaded_by_user_id is not None else None,
        expires_at=(r.expires_at.isoformat() if hasattr(r.expires_at, "isoformat") else (str(r.expires_at) if r.expires_at else None)),
        notes=r.notes,
    )


def enforce_driver_access(user: CurrentUser, driver_id: str) -> None:
    """transport_manager solo puede ver/tocar drivers de su empresa."""
    if user.is_falabella:
        return
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT empresa_id FROM fpoc.drivers WHERE driver_id = ?", driver_id)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "driver no encontrado")
        enforce_fleet_empresa(user, int(row.empresa_id) if row.empresa_id is not None else None)


@router.get("/drivers/{driver_id}/documents", response_model=list[DriverDocOut])
def list_driver_documents(
    driver_id: str,
    user: CurrentUser = Depends(require_fleet_access),
) -> list[DriverDocOut]:
    enforce_driver_access(user, driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT doc_id, driver_id, tipo, filename, file_size, content_type,
                       uploaded_at, uploaded_by_user_id, expires_at, notes
                FROM fpoc.driver_documents
                WHERE driver_id = ?
                ORDER BY uploaded_at DESC""",
            driver_id,
        )
        return [_doc_row(r) for r in cur.fetchall()]


@router.post("/drivers/{driver_id}/documents", response_model=DriverDocOut)
async def upload_driver_document(
    driver_id: str,
    tipo: str = Query(..., description=f"Uno de: {', '.join(DOC_TIPOS)}"),
    expires_at: Optional[str] = Query(default=None, description="YYYY-MM-DD opcional"),
    notes: Optional[str] = Query(default=None, max_length=500),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_fleet_access),
) -> DriverDocOut:
    enforce_driver_access(user, driver_id)
    if tipo not in DOC_TIPOS:
        raise HTTPException(400, f"tipo inválido. Permitidos: {DOC_TIPOS}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "archivo vacío")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "archivo > 25MB")

    import uuid as _uuid
    from core.storage import upload as storage_upload
    safe_name = (file.filename or "documento").replace("/", "_").replace("\\", "_")
    blob_path = f"drivers/{driver_id}/{_uuid.uuid4().hex}_{safe_name}"
    try:
        storage_upload(blob_path, data, content_type=file.content_type)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"storage upload falló: {e}")

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """INSERT INTO fpoc.driver_documents
                (driver_id, tipo, filename, blob_path, file_size, content_type,
                 uploaded_by_user_id, expires_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            driver_id, tipo, safe_name, blob_path, len(data),
            file.content_type, user.user_id, expires_at, notes,
        )
        cn.commit()
        cur.execute(
            """SELECT doc_id, driver_id, tipo, filename, file_size, content_type,
                       uploaded_at, uploaded_by_user_id, expires_at, notes
                FROM fpoc.driver_documents
                WHERE driver_id = ? AND blob_path = ?""",
            driver_id, blob_path,
        )
        return _doc_row(cur.fetchone())


@router.get("/drivers/{driver_id}/documents/{doc_id}/download")
def download_driver_document(
    driver_id: str,
    doc_id: int,
    user: CurrentUser = Depends(require_fleet_access),
) -> StreamingResponse:
    enforce_driver_access(user, driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT filename, blob_path, content_type FROM fpoc.driver_documents "
            "WHERE driver_id = ? AND doc_id = ?",
            driver_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
    from core.storage import download as storage_download
    try:
        data, ct = storage_download(str(row.blob_path))
    except FileNotFoundError:
        raise HTTPException(404, "blob ausente en storage")
    content_type = row.content_type or ct or "application/octet-stream"
    return StreamingResponse(
        io.BytesIO(data),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{row.filename}"'},
    )


@router.delete("/drivers/{driver_id}/documents/{doc_id}")
def delete_driver_document(
    driver_id: str,
    doc_id: int,
    user: CurrentUser = Depends(require_fleet_access),
) -> dict:
    enforce_driver_access(user, driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT blob_path FROM fpoc.driver_documents WHERE driver_id = ? AND doc_id = ?",
            driver_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
        blob_path = str(row.blob_path)
        cur.execute("DELETE FROM fpoc.driver_documents WHERE doc_id = ?", doc_id)
        cn.commit()
    from core.storage import delete as storage_delete
    storage_delete(blob_path)
    return {"deleted": doc_id}

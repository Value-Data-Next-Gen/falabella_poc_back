"""Document management endpoints — upload, list, download, delete, compliance."""
from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.security import current_user
from app.core.security.scope import can_access_empresa
from app.db.models.document import DocumentType, EntityDocument
from app.db.models.driver import Driver
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.session import get_db
from app.schemas.document import (
    ComplianceItem,
    ComplianceReport,
    DocumentTypeOut,
    EntityDocumentOut,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

MAX_FILE_SIZE = 25 * 1024 * 1024

_VALID_ENTITY_TYPES = ("conductor", "vehiculo", "empresa")
# entity_id is concatenated into the storage blob path, so it must contain no
# path separators or traversal sequences (defense against path traversal).
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


async def _authorize_entity(
    db: AsyncSession, user: User, entity_type: str, entity_id: str
) -> int:
    """Validate the entity reference and enforce tenant scope.

    Returns the owning empresa_id. Raises 400 (malformed type/id — also blocks
    path traversal), 404 (entity not found), or 403 (out of tenant scope).

    Every document route is entity-scoped, so this MUST be called before any
    read/write: without it a transport_manager could reach another empresa's
    driver/vehicle PII by guessing entity_type/entity_id.
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "entity_type debe ser conductor, vehiculo o empresa",
        )
    if not _ENTITY_ID_RE.match(entity_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "entity_id inválido")

    if entity_type == "empresa":
        try:
            empresa_id: int | None = int(entity_id)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "entity_id inválido") from None
    elif entity_type == "conductor":
        empresa_id = await db.scalar(
            select(Driver.empresa_id).where(Driver.driver_id == entity_id)
        )
    else:  # vehiculo
        try:
            vehicle_id = int(entity_id)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "entity_id inválido") from None
        empresa_id = await db.scalar(
            select(Vehicle.empresa_id).where(Vehicle.vehicle_id == vehicle_id)
        )

    if empresa_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entidad no encontrada")
    if not can_access_empresa(user, empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sin acceso a esta empresa")
    return empresa_id


@router.get(
    "/types",
    operation_id="listDocumentTypes",
    response_model=list[DocumentTypeOut],
)
async def list_document_types(
    entity_type: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[DocumentTypeOut]:
    stmt = select(DocumentType).where(DocumentType.active == True)  # noqa: E712
    if entity_type:
        stmt = stmt.where(DocumentType.entity_type == entity_type)
    result = await db.execute(stmt.order_by(DocumentType.entity_type, DocumentType.doc_type_id))
    return [DocumentTypeOut.model_validate(r) for r in result.scalars().all()]


@router.get(
    "/{entity_type}/{entity_id}",
    operation_id="listEntityDocuments",
    response_model=list[EntityDocumentOut],
)
async def list_entity_documents(
    entity_type: str,
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[EntityDocumentOut]:
    await _authorize_entity(db, user, entity_type, entity_id)
    result = await db.execute(
        select(EntityDocument)
        .where(EntityDocument.entity_type == entity_type, EntityDocument.entity_id == entity_id)
        .order_by(EntityDocument.uploaded_at.desc())
    )
    return [EntityDocumentOut.model_validate(r) for r in result.scalars().all()]


@router.post(
    "/{entity_type}/{entity_id}",
    operation_id="uploadDocument",
    response_model=EntityDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    entity_type: str,
    entity_id: str,
    file: UploadFile,
    tipo: str = Query(...),
    expires_at: date | None = Query(default=None),
    notes: str | None = Query(default=None, max_length=500),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityDocumentOut:
    await _authorize_entity(db, user, entity_type, entity_id)

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Archivo excede {MAX_FILE_SIZE // (1024*1024)} MB")

    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "bin"
    blob_path = f"{entity_type}s/{entity_id}/{uuid.uuid4().hex}.{ext}"

    storage.upload(blob_path, content, file.content_type)

    doc = EntityDocument(
        entity_type=entity_type,
        entity_id=entity_id,
        tipo=tipo,
        filename=file.filename or "document",
        blob_path=blob_path,
        file_size=len(content),
        content_type=file.content_type,
        uploaded_by_user_id=user.user_id,
        expires_at=expires_at,
        notes=notes,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return EntityDocumentOut.model_validate(doc)


@router.get(
    "/{entity_type}/{entity_id}/{doc_id}/download",
    operation_id="downloadDocument",
)
async def download_document(
    entity_type: str,
    entity_id: str,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    await _authorize_entity(db, user, entity_type, entity_id)
    result = await db.execute(
        select(EntityDocument).where(
            EntityDocument.doc_id == doc_id,
            EntityDocument.entity_type == entity_type,
            EntityDocument.entity_id == entity_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")

    try:
        data, ct = storage.download(doc.blob_path)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado en storage") from None

    return Response(
        content=data,
        media_type=ct or doc.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


@router.delete(
    "/{entity_type}/{entity_id}/{doc_id}",
    operation_id="deleteDocument",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    entity_type: str,
    entity_id: str,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    await _authorize_entity(db, user, entity_type, entity_id)
    result = await db.execute(
        select(EntityDocument).where(
            EntityDocument.doc_id == doc_id,
            EntityDocument.entity_type == entity_type,
            EntityDocument.entity_id == entity_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")

    storage.delete(doc.blob_path)
    await db.delete(doc)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{entity_type}/{entity_id}/compliance",
    operation_id="getDocumentCompliance",
    response_model=ComplianceReport,
)
async def get_compliance(
    entity_type: str,
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> ComplianceReport:
    await _authorize_entity(db, user, entity_type, entity_id)
    types_result = await db.execute(
        select(DocumentType).where(DocumentType.entity_type == entity_type, DocumentType.active == True)  # noqa: E712
    )
    doc_types = types_result.scalars().all()

    docs_result = await db.execute(
        select(EntityDocument).where(
            EntityDocument.entity_type == entity_type,
            EntityDocument.entity_id == entity_id,
        )
    )
    all_docs = docs_result.scalars().all()

    today = datetime.now(UTC).date()
    items: list[ComplianceItem] = []
    total_mandatory = 0
    compliant = 0
    missing = 0
    expired = 0

    for dt in doc_types:
        matching = [d for d in all_docs if d.tipo == dt.codigo]
        latest = max(matching, key=lambda d: d.uploaded_at) if matching else None

        if not matching:
            s = "falta" if dt.mandatory else "opcional"
        elif latest and latest.expires_at and latest.expires_at < today:
            s = "vencido"
        elif latest and latest.expires_at and latest.expires_at < today + timedelta(days=30):
            s = "por_vencer"
        else:
            s = "ok"

        items.append(ComplianceItem(
            doc_type=DocumentTypeOut.model_validate(dt),
            status=s,
            latest_doc=EntityDocumentOut.model_validate(latest) if latest else None,
        ))

        if dt.mandatory:
            total_mandatory += 1
            if s == "ok":
                compliant += 1
            elif s == "falta":
                missing += 1
            elif s == "vencido":
                expired += 1

    return ComplianceReport(
        entity_type=entity_type,
        entity_id=entity_id,
        items=items,
        total_mandatory=total_mandatory,
        compliant=compliant,
        missing=missing,
        expired=expired,
    )

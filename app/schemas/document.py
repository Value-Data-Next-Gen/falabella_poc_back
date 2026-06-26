"""Pydantic schemas for document management."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentTypeOut(BaseModel):
    doc_type_id: int
    entity_type: str
    codigo: str
    nombre: str
    mandatory: bool
    validez_meses: int | None
    active: bool

    model_config = ConfigDict(from_attributes=True)


class EntityDocumentOut(BaseModel):
    doc_id: int
    entity_type: str
    entity_id: str
    tipo: str
    filename: str
    file_size: int
    content_type: str | None
    uploaded_at: datetime
    uploaded_by_user_id: int | None
    expires_at: date | None
    notes: str | None

    model_config = ConfigDict(from_attributes=True)


class ComplianceItem(BaseModel):
    doc_type: DocumentTypeOut
    status: str
    latest_doc: EntityDocumentOut | None = None


class ComplianceReport(BaseModel):
    entity_type: str
    entity_id: str
    items: list[ComplianceItem]
    total_mandatory: int
    compliant: int
    missing: int
    expired: int

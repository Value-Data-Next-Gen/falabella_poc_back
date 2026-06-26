"""Document management: document_types catalog + entity_documents.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None

SEED = [
    ("conductor", "licencia", "Licencia de conducir", True, 60),
    ("conductor", "antecedentes", "Antecedentes", True, 6),
    ("conductor", "contrato", "Contrato laboral", True, None),
    ("conductor", "poliza", "Poliza de seguro", False, 12),
    ("conductor", "certificacion", "Certificacion adicional", False, 24),
    ("vehiculo", "revision_tecnica", "Revision tecnica", True, 12),
    ("vehiculo", "permiso_circ", "Permiso de circulacion", True, 12),
    ("vehiculo", "soap", "SOAP", True, 12),
    ("vehiculo", "poliza", "Poliza de seguro", False, 12),
    ("vehiculo", "padron", "Padron", True, None),
    ("empresa", "contrato_fal", "Contrato Falabella", True, None),
    ("empresa", "rut_doc", "RUT empresa (documento)", True, None),
    ("empresa", "poliza_rc", "Poliza responsabilidad civil", True, 12),
    ("empresa", "cert_vigencia", "Certificado vigencia sociedad", False, 6),
]


def upgrade() -> None:
    op.create_table(
        "document_types",
        sa.Column("doc_type_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("codigo", sa.String(50), nullable=False),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("mandatory", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("validez_meses", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )

    if IS_MSSQL:
        op.execute(f"CREATE UNIQUE INDEX uq_doc_types_entity_codigo ON [{SCHEMA}].[document_types] (entity_type, codigo)")
    else:
        op.create_index("uq_doc_types_entity_codigo", "document_types", ["entity_type", "codigo"], unique=True)

    op.create_table(
        "entity_documents",
        sa.Column("doc_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.String(40), nullable=False),
        sa.Column("tipo", sa.String(50), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("blob_path", sa.String(500), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("uploaded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column("notes", sa.String(500), nullable=True),
        schema=ref_schema,
    )

    if IS_MSSQL:
        op.execute(f"CREATE INDEX ix_entity_docs_lookup ON [{SCHEMA}].[entity_documents] (entity_type, entity_id, tipo)")
    else:
        op.create_index("ix_entity_docs_lookup", "entity_documents", ["entity_type", "entity_id", "tipo"])

    for entity, codigo, nombre, mand, validez in SEED:
        mand_val = 1 if mand else 0
        validez_val = f"{validez}" if validez else "NULL"
        if IS_MSSQL:
            op.execute(
                f"INSERT INTO [{SCHEMA}].[document_types] (entity_type, codigo, nombre, mandatory, validez_meses) "
                f"VALUES ('{entity}', '{codigo}', '{nombre}', {mand_val}, {validez_val})"
            )
        else:
            op.execute(
                f"INSERT INTO document_types (entity_type, codigo, nombre, mandatory, validez_meses) "
                f"VALUES ('{entity}', '{codigo}', '{nombre}', {mand_val}, {validez_val})"
            )


def downgrade() -> None:
    op.drop_table("entity_documents", schema=ref_schema)
    op.drop_table("document_types", schema=ref_schema)

"""Capacitaciones (driver training records).

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "capacitaciones",
        sa.Column("capacitacion_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("driver_id", sa.String(20), nullable=False, index=True),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("institucion", sa.String(200), nullable=True),
        sa.Column("fecha_realizacion", sa.Date(), nullable=True),
        sa.Column("fecha_vencimiento", sa.Date(), nullable=True),
        sa.Column("horas", sa.Integer(), nullable=True),
        sa.Column("estado", sa.String(20), nullable=False, server_default=sa.text("'vigente'")),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )


def downgrade() -> None:
    op.drop_table("capacitaciones", schema=ref_schema)

"""Day lifecycle: dias_operativos + rutas + visitas.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-26 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "dias_operativos",
        sa.Column("dia_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("empresa_id", sa.Integer(), nullable=False, index=True),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default=sa.text("'BORRADOR'")),
        sa.Column("notas", sa.String(500), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("validado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("iniciado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cerrado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )

    if IS_MSSQL:
        op.execute(f"CREATE UNIQUE INDEX uq_dia_empresa_fecha ON [{SCHEMA}].[dias_operativos] (empresa_id, fecha)")

    op.create_table(
        "rutas",
        sa.Column("ruta_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dia_id", sa.Integer(), nullable=False, index=True),
        sa.Column("driver_id", sa.String(20), nullable=False),
        sa.Column("vehicle_id", sa.Integer(), nullable=True),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("notas", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )

    op.create_table(
        "visitas",
        sa.Column("visita_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ruta_id", sa.Integer(), nullable=True, index=True),
        sa.Column("dia_id", sa.Integer(), nullable=False, index=True),
        sa.Column("empresa_id", sa.Integer(), nullable=False, index=True),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cliente_nombre", sa.String(200), nullable=False),
        sa.Column("cliente_rut", sa.String(20), nullable=True),
        sa.Column("cliente_telefono", sa.String(20), nullable=True),
        sa.Column("direccion", sa.String(500), nullable=False),
        sa.Column("comuna", sa.String(100), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("estado", sa.String(20), nullable=False, server_default=sa.text("'pendiente'")),
        sa.Column("motivo", sa.String(100), nullable=True),
        sa.Column("motivo_comentario", sa.String(500), nullable=True),
        sa.Column("motivo_ia_sugerido", sa.String(100), nullable=True),
        sa.Column("eta_estimada", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llegada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("n_bultos", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("referencia", sa.String(100), nullable=True),
        sa.Column("es_vip", sa.Integer(), nullable=True, server_default=sa.text("0")),
        sa.Column("notas", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )


def downgrade() -> None:
    op.drop_table("visitas", schema=ref_schema)
    op.drop_table("rutas", schema=ref_schema)
    op.drop_table("dias_operativos", schema=ref_schema)

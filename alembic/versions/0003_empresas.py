"""empresas (td.empresas)

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-24 00:00:00+00:00

Creates `td.empresas`. Adds FK `users.empresa_id -> empresas.empresa_id`.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings


revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")


def upgrade() -> None:
    op.create_table(
        "empresas",
        sa.Column("empresa_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("razon_social", sa.String(200), nullable=True),
        sa.Column("rut", sa.String(20), nullable=True),
        sa.Column("central_phone", sa.String(20), nullable=True),
        sa.Column("supervisor_phone_e164", sa.String(20), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA if IS_MSSQL else None,
    )

    # Filtered UNIQUE on rut (only when NOT NULL).
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_empresas_rut ON [{SCHEMA}].[empresas] "
            f"(rut) WHERE rut IS NOT NULL"
        )
        # FK users.empresa_id -> empresas.empresa_id
        op.create_foreign_key(
            "fk_users_empresa_id_empresas",
            "users",
            "empresas",
            ["empresa_id"],
            ["empresa_id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
            ondelete="SET NULL",
        )
    else:
        op.create_index(
            "uq_empresas_rut",
            "empresas",
            ["rut"],
            unique=True,
            schema=SCHEMA if IS_MSSQL else None,
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.drop_constraint(
            "fk_users_empresa_id_empresas",
            "users",
            schema=SCHEMA,
            type_="foreignkey",
        )
        op.execute(f"DROP INDEX uq_empresas_rut ON [{SCHEMA}].[empresas]")
    else:
        op.drop_index("uq_empresas_rut", table_name="empresas")
    op.drop_table("empresas", schema=SCHEMA if IS_MSSQL else None)

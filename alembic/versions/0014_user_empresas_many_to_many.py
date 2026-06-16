"""User-empresa many-to-many: junction table td.user_empresas.

Migrates existing users.empresa_id into the junction table, then drops the column.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "user_empresas",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("empresa_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "empresa_id"),
        schema=ref_schema,
    )

    if IS_MSSQL:
        op.execute(
            f"INSERT INTO [{SCHEMA}].[user_empresas] (user_id, empresa_id) "
            f"SELECT user_id, empresa_id FROM [{SCHEMA}].[users] "
            f"WHERE empresa_id IS NOT NULL"
        )


def downgrade() -> None:
    op.drop_table("user_empresas", schema=ref_schema)

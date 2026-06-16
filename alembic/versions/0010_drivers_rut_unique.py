"""Add filtered unique index on drivers.rut (active drivers only).

Prevents the same person (by RUT) from being an active driver in two empresas.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")


def upgrade() -> None:
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_drivers_rut_active "
            f"ON [{SCHEMA}].[drivers] (rut) "
            f"WHERE rut IS NOT NULL AND activo = 1"
        )
    else:
        op.create_index(
            "uq_drivers_rut_active",
            "drivers",
            ["rut"],
            unique=True,
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"DROP INDEX uq_drivers_rut_active ON [{SCHEMA}].[drivers]")
    else:
        op.drop_index("uq_drivers_rut_active", table_name="drivers")

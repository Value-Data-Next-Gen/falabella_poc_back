"""Rename drivers.license → drivers.rut

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")


def upgrade() -> None:
    if IS_MSSQL:
        op.execute(f"EXEC sp_rename '[{SCHEMA}].[drivers].[license]', 'rut', 'COLUMN'")
    else:
        op.alter_column("drivers", "license", new_column_name="rut")


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"EXEC sp_rename '[{SCHEMA}].[drivers].[rut]', 'license', 'COLUMN'")
    else:
        op.alter_column("drivers", "rut", new_column_name="license")

"""Add empresa.region/comuna, vehicle.descripcion, rename driver.license→rut

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.add_column("empresas", sa.Column("region", sa.String(100), nullable=True), schema=ref_schema)
    op.add_column("empresas", sa.Column("comuna", sa.String(100), nullable=True), schema=ref_schema)
    op.add_column("vehicles", sa.Column("descripcion", sa.String(500), nullable=True), schema=ref_schema)


def downgrade() -> None:
    op.drop_column("vehicles", "descripcion", schema=ref_schema)
    op.drop_column("empresas", "comuna", schema=ref_schema)
    op.drop_column("empresas", "region", schema=ref_schema)

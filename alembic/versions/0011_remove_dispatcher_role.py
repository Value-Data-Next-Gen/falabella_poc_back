"""Remove 'dispatcher' role from contactos, migrate existing to 'coordinador'.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-25 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")

OLD_CONSTRAINT = "ck_empresa_contactos_ck_empresa_contactos_rol"


def upgrade() -> None:
    if IS_MSSQL:
        op.execute(f"UPDATE [{SCHEMA}].[empresa_contactos] SET rol='coordinador' WHERE rol='dispatcher'")
        op.execute(f"ALTER TABLE [{SCHEMA}].[empresa_contactos] DROP CONSTRAINT [{OLD_CONSTRAINT}]")
        op.execute(f"ALTER TABLE [{SCHEMA}].[empresa_contactos] ADD CONSTRAINT ck_contactos_rol CHECK (rol IN ('jefe','coordinador','otro'))")


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"ALTER TABLE [{SCHEMA}].[empresa_contactos] DROP CONSTRAINT ck_contactos_rol")
        op.execute(f"ALTER TABLE [{SCHEMA}].[empresa_contactos] ADD CONSTRAINT [{OLD_CONSTRAINT}] CHECK (rol IN ('jefe','coordinador','dispatcher','otro'))")

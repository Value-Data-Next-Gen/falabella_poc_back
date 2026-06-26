"""vehicles (td.vehicles)

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-24 00:00:00+00:00

Creates `td.vehicles` with FK to `td.empresas` (ON DELETE CASCADE).
Filtered UNIQUE on `plate` (WHERE NOT NULL) on MSSQL.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings


revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "vehicles",
        sa.Column("vehicle_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id",
            sa.Integer(),
            sa.ForeignKey(
                f"{SCHEMA + '.' if IS_MSSQL else ''}empresas.empresa_id",
                ondelete="CASCADE",
                name="fk_vehicles_empresa_id_empresas",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("plate", sa.String(20), nullable=True),
        sa.Column("tipo", sa.String(50), nullable=True),
        sa.Column("capacity_m3", sa.Integer(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("depot_lat", sa.Float(), nullable=True),
        sa.Column("depot_lon", sa.Float(), nullable=True),
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
        schema=ref_schema,
    )

    # Filtered UNIQUE on plate (only when NOT NULL).
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_vehicles_plate ON [{SCHEMA}].[vehicles] "
            f"(plate) WHERE plate IS NOT NULL"
        )
    else:
        op.create_index(
            "uq_vehicles_plate",
            "vehicles",
            ["plate"],
            unique=True,
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"DROP INDEX uq_vehicles_plate ON [{SCHEMA}].[vehicles]")
    else:
        op.drop_index("uq_vehicles_plate", table_name="vehicles")
    op.drop_table("vehicles", schema=ref_schema)

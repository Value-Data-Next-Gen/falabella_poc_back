"""drivers (td.drivers)

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-24 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings


revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "drivers",
        sa.Column("driver_id", sa.String(20), primary_key=True),
        sa.Column(
            "empresa_id",
            sa.Integer(),
            sa.ForeignKey(
                f"{SCHEMA + '.' if IS_MSSQL else ''}empresas.empresa_id",
                ondelete="CASCADE",
                name="fk_drivers_empresa_id_empresas",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey(
                f"{SCHEMA + '.' if IS_MSSQL else ''}vehicles.vehicle_id",
                ondelete="NO ACTION",  # MSSQL forbids multi-cascade paths via empresa
                name="fk_drivers_vehicle_id_vehicles",
            ),
            nullable=True,
            index=True,
        ),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("license", sa.String(50), nullable=True),
        sa.Column("phone_e164", sa.String(20), nullable=True),
        sa.Column("notify_whatsapp", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("opted_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activation_token", sa.String(32), nullable=True),
        sa.Column("activation_used_at", sa.DateTime(timezone=True), nullable=True),
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

    # Filtered UNIQUE indexes on MSSQL (allow NULL but unique non-NULL).
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_drivers_phone_e164 ON [{SCHEMA}].[drivers] "
            f"(phone_e164) WHERE phone_e164 IS NOT NULL"
        )
        op.execute(
            f"CREATE UNIQUE INDEX uq_drivers_activation_token ON [{SCHEMA}].[drivers] "
            f"(activation_token) WHERE activation_token IS NOT NULL"
        )
    else:
        op.create_index("uq_drivers_phone_e164", "drivers", ["phone_e164"], unique=True)
        op.create_index("uq_drivers_activation_token", "drivers", ["activation_token"], unique=True)


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"DROP INDEX uq_drivers_activation_token ON [{SCHEMA}].[drivers]")
        op.execute(f"DROP INDEX uq_drivers_phone_e164 ON [{SCHEMA}].[drivers]")
    else:
        op.drop_index("uq_drivers_activation_token", table_name="drivers")
        op.drop_index("uq_drivers_phone_e164", table_name="drivers")
    op.drop_table("drivers", schema=ref_schema)

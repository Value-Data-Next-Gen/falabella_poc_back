"""empresa_contactos (td.empresa_contactos)

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-24 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings


revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "empresa_contactos",
        sa.Column("contact_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id",
            sa.Integer(),
            sa.ForeignKey(
                f"{SCHEMA + '.' if IS_MSSQL else ''}empresas.empresa_id",
                ondelete="CASCADE",
                name="fk_empresa_contactos_empresa_id_empresas",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("rol", sa.String(50), nullable=False),
        sa.Column("phone_e164", sa.String(20), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("opted_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activation_token", sa.String(32), nullable=True),
        sa.Column("activation_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_severities", sa.String(100), nullable=True),
        sa.Column("notify_motivos", sa.String(500), nullable=True),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey(
                f"{SCHEMA + '.' if IS_MSSQL else ''}users.user_id",
                ondelete="NO ACTION",
                name="fk_empresa_contactos_created_by_user_id_users",
            ),
            nullable=True,
        ),
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
        sa.CheckConstraint(
            "rol IN ('jefe','coordinador','dispatcher','otro')",
            name="ck_empresa_contactos_rol",
        ),
        schema=ref_schema,
    )

    # Filtered UNIQUE indexes
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_empresa_contactos_empresa_phone "
            f"ON [{SCHEMA}].[empresa_contactos] (empresa_id, phone_e164) "
            f"WHERE phone_e164 IS NOT NULL AND activo = 1"
        )
        op.execute(
            f"CREATE UNIQUE INDEX uq_empresa_contactos_activation_token "
            f"ON [{SCHEMA}].[empresa_contactos] (activation_token) "
            f"WHERE activation_token IS NOT NULL"
        )
    else:
        op.create_index(
            "uq_empresa_contactos_empresa_phone",
            "empresa_contactos",
            ["empresa_id", "phone_e164"],
            unique=True,
        )
        op.create_index(
            "uq_empresa_contactos_activation_token",
            "empresa_contactos",
            ["activation_token"],
            unique=True,
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"DROP INDEX uq_empresa_contactos_activation_token ON [{SCHEMA}].[empresa_contactos]")
        op.execute(f"DROP INDEX uq_empresa_contactos_empresa_phone ON [{SCHEMA}].[empresa_contactos]")
    else:
        op.drop_index("uq_empresa_contactos_activation_token", table_name="empresa_contactos")
        op.drop_index("uq_empresa_contactos_empresa_phone", table_name="empresa_contactos")
    op.drop_table("empresa_contactos", schema=ref_schema)

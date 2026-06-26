"""users (td.users)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-24 00:00:00+00:00

Creates `td.users` (schema `td` is also created if missing). On SQLite (tests),
the schema is ignored — SQLAlchemy maps `td.users` → `users` and the test runs
clean.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings


revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")


def upgrade() -> None:
    # 1. Ensure schema exists (MSSQL only — SQLite has no schemas).
    if IS_MSSQL:
        op.execute(
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'{SCHEMA}') "
            f"EXEC('CREATE SCHEMA [{SCHEMA}]')"
        )

    # 2. Create users table.
    op.create_table(
        "users",
        sa.Column("user_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("empresa_id", sa.Integer(), nullable=True),
        sa.Column("driver_id", sa.String(20), nullable=True),
        sa.Column("phone_e164", sa.String(20), nullable=True),
        sa.Column(
            "notify_whatsapp", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
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
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            "role IN ('falabella_admin','falabella_ops','transport_manager','driver')",
            name="ck_users_role",
        ),
        schema=SCHEMA if IS_MSSQL else None,
    )

    # 3. Index on activation_token (NULL allowed; uniqueness only when present).
    # MSSQL filtered index; SQLite gets a plain non-unique index (good enough for tests).
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX ix_users_activation_token ON [{SCHEMA}].[users] "
            f"(activation_token) WHERE activation_token IS NOT NULL"
        )
    else:
        op.create_index(
            "ix_users_activation_token",
            "users",
            ["activation_token"],
            schema=SCHEMA if IS_MSSQL else None,
        )

    # 4. Bootstrap admin user (only in dev; idempotent).
    # Hardcoded hash of "admin123" via bcrypt; for prod, replace via PATCH or env.
    from app.core.security.passwords import hash_password

    pwd_hash = hash_password(settings.initial_admin_password)
    op.execute(
        sa.text(
            f"INSERT INTO {SCHEMA + '.' if IS_MSSQL else ''}users "
            "(email, password_hash, display_name, role, activo) "
            "VALUES (:email, :pwd, :name, :role, 1)"
        ).bindparams(
            email="admin@td.cl",
            pwd=pwd_hash,
            name="Admin v2",
            role="falabella_admin",
        )
    )


def downgrade() -> None:
    op.drop_index("ix_users_activation_token", table_name="users", schema=SCHEMA if IS_MSSQL else None)
    op.drop_table("users", schema=SCHEMA if IS_MSSQL else None)

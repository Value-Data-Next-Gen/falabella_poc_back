"""Simulation clock singleton.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-29 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.create_table(
        "sim_clock",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("sim_now", sa.DateTime(timezone=True), nullable=False),
        sa.Column("speed", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("running", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )
    if IS_MSSQL:
        op.execute(
            f"INSERT INTO [{SCHEMA}].[sim_clock] (id, sim_now, speed, running) "
            f"VALUES (1, GETUTCDATE(), 1.0, 0)"
        )


def downgrade() -> None:
    op.drop_table("sim_clock", schema=ref_schema)

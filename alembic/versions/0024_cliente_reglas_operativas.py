"""CR-024: cliente reglas operativas (ventana horaria, dias no disponible, prioridad).

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-30 21:00:00+00:00

Motivation:
  CR-024 connects the cliente master to the live operation. Beyond the existing
  VIP / notas_operativas / direccion_default fields, operators need to encode
  per-cliente operational rules:
    - `ventana_horaria_inicio` / `ventana_horaria_fin` — preferred delivery
      window (HH:MM). Used by the LLM bot to advise drivers and (in a later CR)
      by the planner to filter feasible slots.
    - `dias_no_disponible` — JSON array of ISO weekday codes the cliente cannot
      receive deliveries (e.g. `["sat","sun"]`).
    - `prioridad` — 1..5 (1 highest, 5 lowest). Used to escalate severity in
      the eta_breach cron and to tie-break planning.

  All four columns are NULLABLE (no default). Existing clientes keep their
  current behavior; opt-in per cliente.

DDL changes:
  - ADD COLUMN ventana_horaria_inicio TIME NULL
  - ADD COLUMN ventana_horaria_fin TIME NULL
  - ADD COLUMN dias_no_disponible NVARCHAR(50) NULL  (JSON array as text)
  - ADD COLUMN prioridad INT NULL

  Validation of `prioridad` (1..5) and `dias_no_disponible` enum is done in
  Pydantic, not via DB CHECK — keeps the migration portable and tolerates
  hand-written values.

Idempotency: Alembic tracks revisions; this runs once. On SQLite (test path)
the columns are added the same way; Base.metadata.create_all already declares
them so test setups via `create_all` are unaffected.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.add_column(
        "clientes",
        sa.Column("ventana_horaria_inicio", sa.Time(), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "clientes",
        sa.Column("ventana_horaria_fin", sa.Time(), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "clientes",
        sa.Column("dias_no_disponible", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "clientes",
        sa.Column("prioridad", sa.Integer(), nullable=True),
        schema=ref_schema,
    )


def downgrade() -> None:
    op.drop_column("clientes", "prioridad", schema=ref_schema)
    op.drop_column("clientes", "dias_no_disponible", schema=ref_schema)
    op.drop_column("clientes", "ventana_horaria_fin", schema=ref_schema)
    op.drop_column("clientes", "ventana_horaria_inicio", schema=ref_schema)

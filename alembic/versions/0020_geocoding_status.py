"""Add geocoding_status / geocoding_attempts / geocoded_at to td.clientes (CR-020).

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-30 00:00:00+00:00

Background (CR-020):
  The CR-019 ingest left `td.clientes.lat_default/lon_default` NULL on import and
  relied on a fire-and-forget `asyncio.create_task` to back-fill them via
  Nominatim. That task did not survive uvicorn worker restarts, so on Azure
  App Service only ~25/2119 clientes ever got geocoded. The new flow:

    * ingest sets `geocoding_status = 'centroide_fallback'` immediately,
      populating lat/lon with the comuna centroid synchronously;
    * a lifespan-owned background loop in `app/main.py` walks pending +
      centroide rows and upgrades them to `geocoding_status = 'nominatim_ok'`
      (or `'failed'` after 3 attempts);
    * an admin endpoint `POST /api/v1/admin/geocoding/run` reprocesses
      pending rows on demand.

Columns:
  * `geocoding_status` — `'pending'` | `'centroide_fallback'` | `'nominatim_ok'`
    | `'failed'`. NOT NULL, default `'pending'` so any rows pre-existing this
    migration are eligible for the background loop.
  * `geocoding_attempts` — int counter, NOT NULL default 0. Background loop
    increments on each Nominatim call so we stop at MAX_ATTEMPTS (3).
  * `geocoded_at` — timestamp of last successful Nominatim resolution.

Idempotency: Alembic tracks revisions so this migration only runs once. Pure
ADD COLUMN, no destructive change.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def upgrade() -> None:
    op.add_column(
        "clientes",
        sa.Column(
            "geocoding_status",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        schema=ref_schema,
    )
    op.add_column(
        "clientes",
        sa.Column(
            "geocoding_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=ref_schema,
    )
    op.add_column(
        "clientes",
        sa.Column(
            "geocoded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        schema=ref_schema,
    )
    # Filtered index makes the lifespan-loop SELECT pending/centroide cheap.
    if IS_MSSQL:
        op.execute(
            f"CREATE INDEX ix_clientes_geocoding_status "
            f"ON [{SCHEMA}].[clientes] (geocoding_status) "
            f"WHERE geocoding_status IN ('pending', 'centroide_fallback')"
        )
    else:
        op.create_index(
            "ix_clientes_geocoding_status",
            "clientes",
            ["geocoding_status"],
            unique=False,
            schema=ref_schema,
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"DROP INDEX ix_clientes_geocoding_status ON [{SCHEMA}].[clientes]")
    else:
        op.drop_index(
            "ix_clientes_geocoding_status", table_name="clientes", schema=ref_schema
        )
    op.drop_column("clientes", "geocoded_at", schema=ref_schema)
    op.drop_column("clientes", "geocoding_attempts", schema=ref_schema)
    op.drop_column("clientes", "geocoding_status", schema=ref_schema)

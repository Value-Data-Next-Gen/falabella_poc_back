"""CR-027 Part A: drop td.cliente_empresas + clientes.empresa_id column.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-31 00:00:00+00:00

Motivation:
  CR-023 introduced `td.cliente_empresas` (M2M) and kept `td.clientes.empresa_id`
  nullable as a "global cliente served by N transportistas" model. In practice,
  the relationship cliente <-> empresa is *always* derivable from operational
  data:

      empresa <- dias_operativos <- rutas <- visitas -> cliente

  The M2M table was a denormalization of that chain, with counters that drift
  every time we change ingest semantics. Worse, it baked a contract leak — the
  master `cliente` shouldn't carry tenant identity at all.

  CR-027 restores the correct model: cliente master = identity-only; tenant
  links are computed on demand from visitas joins. This migration drops both
  the M2M table and the residual `clientes.empresa_id` column.

DDL changes (in order):
  1. DROP TABLE td.cliente_empresas (CASCADEs the 2 FKs + the lastseen index).
  2. Drop FK `fk_clientes_empresa_id_empresas` if it still exists.
  3. Drop index `ix_clientes_empresa_id` if it still exists.
  4. ALTER TABLE td.clientes DROP COLUMN empresa_id.

The global filtered unique index on `clientes.rut` (CR-023) is preserved.

Downgrade:
  Recreates an EMPTY `td.cliente_empresas` table (counters lost) and re-adds the
  `clientes.empresa_id` column NULLABLE with the FK back. We do not backfill —
  the data is irrecoverable.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def _qual(table: str) -> str:
    if IS_MSSQL:
        return f"[{SCHEMA}].[{table}]" if SCHEMA else f"[{table}]"
    return f'"{table}"'


def _fk(table: str, col: str) -> str:
    return f"{SCHEMA + '.' if IS_MSSQL and SCHEMA else ''}{table}.{col}"


def upgrade() -> None:
    # ── 1. Drop cliente_empresas table (with its supporting index) ─────
    if IS_MSSQL:
        # Defensive: drop the index first; some MSSQL versions complain when
        # dropping a table with active indexes that reference foreign objects.
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.indexes "
            f"WHERE name='ix_cliente_empresas_empresa_lastseen' "
            f"AND object_id = OBJECT_ID('{SCHEMA}.cliente_empresas')) "
            f"DROP INDEX ix_cliente_empresas_empresa_lastseen "
            f"ON {_qual('cliente_empresas')}"
        )
        op.execute(
            f"IF OBJECT_ID('{SCHEMA}.cliente_empresas', 'U') IS NOT NULL "
            f"DROP TABLE {_qual('cliente_empresas')}"
        )
    else:
        # SQLite path — drop_index tolerates missing index via if_exists=True
        # but the Alembic API does not always expose that. Use raw SQL.
        op.execute('DROP INDEX IF EXISTS ix_cliente_empresas_empresa_lastseen')
        op.execute('DROP TABLE IF EXISTS cliente_empresas')

    # ── 2. Drop the FK on clientes.empresa_id ──────────────────────────
    if IS_MSSQL:
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.foreign_keys "
            f"WHERE name='fk_clientes_empresa_id_empresas') "
            f"ALTER TABLE {_qual('clientes')} "
            f"DROP CONSTRAINT fk_clientes_empresa_id_empresas"
        )

    # ── 3. Drop ALL indexes on clientes that reference empresa_id ──────
    # The index name varies by source of creation (autogenerate uses
    # `ix_<schema>_<table>_<col>`, hand-rolled migrations use the unprefixed
    # form). Discover by joining sys.index_columns and drop dynamically.
    if IS_MSSQL:
        op.execute(
            f"""
            DECLARE @drop_sql NVARCHAR(MAX) = N'';
            SELECT @drop_sql = @drop_sql +
                'DROP INDEX ' + QUOTENAME(i.name) +
                ' ON ' + QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' +
                QUOTENAME(t.name) + ';' + CHAR(10)
            FROM sys.indexes i
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.index_columns ic
                 ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
                 ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE t.object_id = OBJECT_ID('{SCHEMA}.clientes')
              AND c.name = 'empresa_id'
              AND i.is_primary_key = 0
              AND i.is_unique_constraint = 0;
            IF LEN(@drop_sql) > 0 EXEC sp_executesql @drop_sql;
            """
        )

    # ── 4. Drop the column itself ──────────────────────────────────────
    # On MSSQL, ALTER TABLE DROP COLUMN fails if a default constraint exists.
    # CR-023's column has no server_default but old rows may have one named
    # via the original 0019 migration. Defensive: drop any default constraint
    # bound to empresa_id first.
    if IS_MSSQL:
        op.execute(
            f"""
            DECLARE @sql NVARCHAR(MAX);
            SELECT @sql = 'ALTER TABLE {_qual('clientes')} DROP CONSTRAINT ' + dc.name
            FROM sys.default_constraints dc
            JOIN sys.columns c ON c.default_object_id = dc.object_id
            WHERE dc.parent_object_id = OBJECT_ID('{SCHEMA}.clientes')
              AND c.name = 'empresa_id';
            IF @sql IS NOT NULL EXEC sp_executesql @sql;
            """
        )
    op.drop_column("clientes", "empresa_id", schema=ref_schema)


def downgrade() -> None:
    # ── 1. Re-add clientes.empresa_id NULLABLE with FK ─────────────────
    op.add_column(
        "clientes",
        sa.Column("empresa_id", sa.Integer(), nullable=True),
        schema=ref_schema,
    )
    op.create_index(
        "ix_clientes_empresa_id",
        "clientes",
        ["empresa_id"],
        unique=False,
        schema=ref_schema,
    )
    if IS_MSSQL:
        op.execute(
            f"ALTER TABLE {_qual('clientes')} "
            f"ADD CONSTRAINT fk_clientes_empresa_id_empresas "
            f"FOREIGN KEY (empresa_id) REFERENCES {_qual('empresas')} (empresa_id) "
            f"ON DELETE CASCADE"
        )

    # ── 2. Recreate cliente_empresas (EMPTY — data lost) ───────────────
    op.create_table(
        "cliente_empresas",
        sa.Column(
            "cliente_id",
            sa.Integer(),
            sa.ForeignKey(
                _fk("clientes", "cliente_id"),
                name="fk_cliente_empresas_cliente_id_clientes",
                ondelete="CASCADE",
            ),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "empresa_id",
            sa.Integer(),
            sa.ForeignKey(
                _fk("empresas", "empresa_id"),
                name="fk_cliente_empresas_empresa_id_empresas",
                ondelete="NO ACTION",  # MSSQL multi-cascade-path constraint.
            ),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "visitas_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "entregas_ok", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "entregas_fallidas",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=ref_schema,
    )
    op.create_index(
        "ix_cliente_empresas_empresa_lastseen",
        "cliente_empresas",
        ["empresa_id", "last_seen_at"],
        unique=False,
        schema=ref_schema,
    )

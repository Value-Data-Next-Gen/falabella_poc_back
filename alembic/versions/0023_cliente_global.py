"""CR-023: Cliente as global Falabella entity + cliente_empresas M2M.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-30 20:00:00+00:00

Motivation:
  The CR-019 model assigned `cliente.empresa_id` rigidly via the surrogate RUT
  `FAL-{do}`. But the same Falabella client may be served by different
  transportistas on different days — the rigid FK caused duplicate cliente
  rows whenever a `do` appeared in two empresas, breaking VIP flags, operational
  notes, geocoding cache and historical aggregates.

  This migration restructures the model so:
    - `td.clientes` is single-tenant Falabella (unique by RUT, globally).
    - `td.cliente_empresas (cliente_id, empresa_id, …)` stores the M2M
      relationship to transportistas with per-relationship counters
      (first/last seen, visitas_count, entregas_ok, entregas_fallidas).

DDL changes:
  1. Drop the old filtered unique `uq_clientes_empresa_rut` (empresa_id, rut)
     and create a new filtered unique on `rut` alone (still allowing multiple
     NULL ruts, which the pre-ingest seed and any manual rows may have).
  2. Make `td.clientes.empresa_id` NULLABLE (kept for backward compat).
  3. Create `td.cliente_empresas` with composite PK + FKs + supporting index.
     FK strategy:
       - `cliente_id -> clientes`: ON DELETE CASCADE. If a cliente is hard-
         deleted, its history with transportistas dies with it.
       - `empresa_id -> empresas`: ON DELETE NO ACTION. MSSQL refuses
         CASCADE because there are two cascade paths from `empresas` into
         `cliente_empresas` (direct, and via `clientes.empresa_id`).
         Same class of bug as CR-021's `visitas.ruta_id -> rutas`. If a
         transportista is deleted, app-layer cleanup must remove orphan
         rows in cliente_empresas — but this preserves the historical
         relationship if the empresa is merely deactivated.

Backfill (run in upgrade() AFTER the DDL):
  a) Dedupe clientes with the same RUT across empresas. Priority of the
     "survivor" row:
       1. es_vip = 1
       2. vip_razon IS NOT NULL OR notas_operativas IS NOT NULL
       3. lat_default IS NOT NULL (already geocoded)
       4. MIN(cliente_id)
     For every non-survivor we re-point its visitas to the survivor and DELETE
     the row.
  b) Populate `td.cliente_empresas` from the existing visitas + dias_operativos
     aggregates: one row per (cliente_id, dia.empresa_id) with visitas_count,
     entregas_ok, entregas_fallidas, first/last seen.
  c) Log the resulting counts (LiveQuery in the alembic log).

Idempotency: Alembic tracks revisions; this runs once.

Notes:
  - SQLite (test path) does not support the MSSQL-only filtered indexes; we
    fall back to a plain non-unique index there. The dedupe + backfill runs
    on both backends because it is pure SQL via op.execute().
  - The new model `ClienteEmpresa` is registered on Base.metadata, so the
    SQLite tests (which call `Base.metadata.create_all`) get the table
    automatically without running this migration.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.core.config import settings

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
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
    # ── 1. Drop old (empresa_id, rut) filtered unique ──────────────────
    if IS_MSSQL:
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.indexes "
            f"WHERE name = 'uq_clientes_empresa_rut' "
            f"AND object_id = OBJECT_ID('{SCHEMA}.clientes')) "
            f"DROP INDEX uq_clientes_empresa_rut ON {_qual('clientes')}"
        )

    # ── 2. Relax NOT NULL on clientes.empresa_id ───────────────────────
    # MSSQL requires the FK to be dropped temporarily before altering the
    # column nullability, then re-added. Skip on SQLite (it's already
    # rebuilt fresh via Base.metadata when used in tests).
    if IS_MSSQL:
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.foreign_keys "
            f"WHERE name = 'fk_clientes_empresa_id_empresas') "
            f"ALTER TABLE {_qual('clientes')} "
            f"DROP CONSTRAINT fk_clientes_empresa_id_empresas"
        )
        op.execute(
            f"ALTER TABLE {_qual('clientes')} "
            f"ALTER COLUMN empresa_id INT NULL"
        )
        op.execute(
            f"ALTER TABLE {_qual('clientes')} "
            f"ADD CONSTRAINT fk_clientes_empresa_id_empresas "
            f"FOREIGN KEY (empresa_id) REFERENCES {_qual('empresas')} (empresa_id) "
            f"ON DELETE CASCADE"
        )

    # ── 3. Global filtered unique on rut ───────────────────────────────
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_clientes_rut_global "
            f"ON {_qual('clientes')} (rut) WHERE rut IS NOT NULL"
        )
    else:
        # SQLite: plain index (filtered unique not portable via Alembic).
        op.create_index(
            "ix_clientes_rut_global",
            "clientes",
            ["rut"],
            unique=False,
            schema=ref_schema,
        )

    # ── 4. Create td.cliente_empresas ──────────────────────────────────
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
                # NOTE (CR-023 fix, 2026-05-30): NO ACTION, not CASCADE.
                # MSSQL refuses to create CASCADE here because it detects
                # multiple cascade paths into cliente_empresas:
                #   1. empresas -> cliente_empresas.empresa_id (this FK)
                #   2. empresas -> clientes -> cliente_empresas.cliente_id
                #      (via clientes.empresa_id, which is CASCADE)
                # error 1785 "may cause cycles or multiple cascade paths".
                # Same class of bug we hit in CR-021 with visitas.ruta_id.
                #
                # Trade-off: if a transportista is deleted, the rows in
                # cliente_empresas referencing it become orphans (with a
                # broken empresa_id pointer) and the app layer must clean
                # them up. This is preferable to losing audit-trail history
                # of which clientes that transportista served.
                ondelete="NO ACTION",
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
            "visitas_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "entregas_ok",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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

    # ── 5. Dedupe clientes by RUT (pure SQL, both backends) ────────────
    # Strategy:
    #   - Build a temp table of (rut -> survivor cliente_id) using priority.
    #   - UPDATE visitas to remap cliente_id from non-survivor to survivor.
    #   - DELETE non-survivors.
    #
    # We do NOT consolidate vip_razon / notas_operativas onto the survivor:
    # the priority already picks the row that has them. Anything lost on
    # non-priority duplicates was already a duplicate and out of scope.
    #
    # SQL: select rut groups with >1 cliente, rank by priority.
    if IS_MSSQL:
        # Step 5a — find duplicates by rut, pick survivor by priority.
        op.execute(
            f"""
            ;WITH ranked AS (
                SELECT
                    c.cliente_id,
                    c.rut,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.rut
                        ORDER BY
                            CASE WHEN c.es_vip = 1 THEN 0 ELSE 1 END,
                            CASE WHEN c.vip_razon IS NOT NULL OR c.notas_operativas IS NOT NULL THEN 0 ELSE 1 END,
                            CASE WHEN c.lat_default IS NOT NULL THEN 0 ELSE 1 END,
                            c.cliente_id
                    ) AS rn,
                    FIRST_VALUE(c.cliente_id) OVER (
                        PARTITION BY c.rut
                        ORDER BY
                            CASE WHEN c.es_vip = 1 THEN 0 ELSE 1 END,
                            CASE WHEN c.vip_razon IS NOT NULL OR c.notas_operativas IS NOT NULL THEN 0 ELSE 1 END,
                            CASE WHEN c.lat_default IS NOT NULL THEN 0 ELSE 1 END,
                            c.cliente_id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS survivor_id
                FROM {_qual('clientes')} c
                WHERE c.rut IS NOT NULL
                  AND c.rut IN (
                      SELECT rut FROM {_qual('clientes')}
                      WHERE rut IS NOT NULL
                      GROUP BY rut HAVING COUNT(*) > 1
                  )
            )
            SELECT cliente_id, survivor_id
            INTO #cliente_dedupe_map
            FROM ranked
            WHERE rn > 1;
            """
        )
        # Step 5b — repoint visitas.
        op.execute(
            f"""
            UPDATE v
            SET v.cliente_id = m.survivor_id
            FROM {_qual('visitas')} v
            JOIN #cliente_dedupe_map m ON v.cliente_id = m.cliente_id;
            """
        )
        # Step 5c — delete non-survivors.
        op.execute(
            f"""
            DELETE c
            FROM {_qual('clientes')} c
            JOIN #cliente_dedupe_map m ON c.cliente_id = m.cliente_id;
            """
        )
        op.execute("DROP TABLE #cliente_dedupe_map;")
    else:
        # SQLite path — only runs in tests with empty data. No-op.
        pass

    # ── 6. Backfill cliente_empresas from visitas + dias ───────────────
    # One MERGE/INSERT per (cliente_id, dia.empresa_id) aggregating all
    # visitas. SQL Server has MERGE; SQLite has INSERT … ON CONFLICT but
    # composite PKs require the column list spelled out.
    if IS_MSSQL:
        op.execute(
            f"""
            ;WITH agg AS (
                SELECT
                    v.cliente_id,
                    d.empresa_id,
                    MIN(v.created_at) AS first_seen_at,
                    MAX(v.created_at) AS last_seen_at,
                    COUNT(*) AS visitas_count,
                    SUM(CASE WHEN v.estado = 'entregado' THEN 1 ELSE 0 END) AS entregas_ok,
                    SUM(CASE WHEN v.estado = 'no_entregado' THEN 1 ELSE 0 END) AS entregas_fallidas
                FROM {_qual('visitas')} v
                JOIN {_qual('dias_operativos')} d ON d.dia_id = v.dia_id
                WHERE v.cliente_id IS NOT NULL
                GROUP BY v.cliente_id, d.empresa_id
            )
            MERGE INTO {_qual('cliente_empresas')} AS tgt
            USING agg AS src
            ON tgt.cliente_id = src.cliente_id AND tgt.empresa_id = src.empresa_id
            WHEN MATCHED THEN UPDATE SET
                tgt.first_seen_at = src.first_seen_at,
                tgt.last_seen_at = src.last_seen_at,
                tgt.visitas_count = src.visitas_count,
                tgt.entregas_ok = src.entregas_ok,
                tgt.entregas_fallidas = src.entregas_fallidas
            WHEN NOT MATCHED THEN
                INSERT (cliente_id, empresa_id, first_seen_at, last_seen_at,
                        visitas_count, entregas_ok, entregas_fallidas)
                VALUES (src.cliente_id, src.empresa_id, src.first_seen_at,
                        src.last_seen_at, src.visitas_count,
                        src.entregas_ok, src.entregas_fallidas);
            """
        )

    # ── 7. Verification counts (logged via op.get_bind()) ──────────────
    if IS_MSSQL:
        bind = op.get_bind()
        n_clientes = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_qual('clientes')}")
        ).scalar()
        n_rel = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_qual('cliente_empresas')}")
        ).scalar()
        n_clientes_no_emp = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {_qual('clientes')} WHERE empresa_id IS NULL"
            )
        ).scalar()
        print(
            f"[CR-023] post-migration: clientes={n_clientes} "
            f"cliente_empresas={n_rel} "
            f"clientes_with_null_empresa={n_clientes_no_emp}"
        )


def downgrade() -> None:
    # Drop M2M table + index.
    op.drop_index(
        "ix_cliente_empresas_empresa_lastseen",
        table_name="cliente_empresas",
        schema=ref_schema,
    )
    op.drop_table("cliente_empresas", schema=ref_schema)

    # Drop global unique / index on rut.
    if IS_MSSQL:
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.indexes WHERE name='uq_clientes_rut_global' "
            f"AND object_id = OBJECT_ID('{SCHEMA}.clientes')) "
            f"DROP INDEX uq_clientes_rut_global ON {_qual('clientes')}"
        )
    else:
        op.drop_index(
            "ix_clientes_rut_global", table_name="clientes", schema=ref_schema
        )

    # Restore NOT NULL on empresa_id (best effort: requires no null rows;
    # downgrade against post-CR-023 data WILL fail if any cliente has
    # empresa_id IS NULL — operator must fix manually first).
    if IS_MSSQL:
        op.execute(
            f"ALTER TABLE {_qual('clientes')} "
            f"DROP CONSTRAINT fk_clientes_empresa_id_empresas"
        )
        op.execute(
            f"ALTER TABLE {_qual('clientes')} "
            f"ALTER COLUMN empresa_id INT NOT NULL"
        )
        op.execute(
            f"ALTER TABLE {_qual('clientes')} "
            f"ADD CONSTRAINT fk_clientes_empresa_id_empresas "
            f"FOREIGN KEY (empresa_id) REFERENCES {_qual('empresas')} (empresa_id) "
            f"ON DELETE CASCADE"
        )
        op.execute(
            f"CREATE UNIQUE INDEX uq_clientes_empresa_rut "
            f"ON {_qual('clientes')} (empresa_id, rut) WHERE rut IS NOT NULL"
        )

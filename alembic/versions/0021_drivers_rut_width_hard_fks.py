"""CR-021: shrink drivers.rut to varchar(20) + add hard FKs across td.* tables.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-30 12:00:00+00:00

Background (CR-021):
  Audit found:
    * `drivers.rut` is `varchar(50)` in DB but ORM declares `String(20)` and
      Pydantic `max_length=20`. Width is harmlessly wider in DB; we narrow it
      to match ORM/contract. Pre-check fails the migration if any existing
      row exceeds 20 chars.
    * Several FK relationships only exist in the ORM (no DB-level constraint).
      We add them with explicit ON DELETE semantics so the DB enforces tenant
      integrity even when applications skip helpers.

  Per-child orphan probes run BEFORE each FK is added. If a probe finds any
  orphans the migration aborts with a clear message (no silent constraint
  failure halfway through).

drivers.rut narrowing — index dependency (CR-021 v2):
  Migration 0010 created a filtered UNIQUE index `uq_drivers_rut_active` on
  `(rut)` WHERE `rut IS NOT NULL AND activo = 1`. SQL Server rejects
  ALTER COLUMN while any index references the column, so we DROP the index,
  ALTER, then re-CREATE it with the identical filter. The filter clause was
  read straight from `sys.indexes.filter_definition`, which returns the
  normalized form `([rut] IS NOT NULL AND [activo]=(1))` — we use the
  human form `rut IS NOT NULL AND activo = 1` which is semantically equal.

FK matrix added in this migration (all `td.*` references):

  | child.column                | parent.column        | ON DELETE  | nullable |
  |-----------------------------|----------------------|------------|----------|
  | dias_operativos.empresa_id  | empresas.empresa_id  | NO ACTION  | N        |
  | rutas.dia_id                | dias_operativos.dia_id | CASCADE  | N        |
  | rutas.driver_id             | drivers.driver_id    | NO ACTION  | N        |
  | rutas.vehicle_id            | vehicles.vehicle_id  | NO ACTION  | Y        |
  | visitas.dia_id              | dias_operativos.dia_id | CASCADE  | N        |
  | visitas.ruta_id             | rutas.ruta_id        | NO ACTION  | Y        |  ← see note 1
  | visitas.empresa_id          | empresas.empresa_id  | NO ACTION  | N        |
  | user_empresas.user_id       | users.user_id        | CASCADE    | N        |
  | user_empresas.empresa_id    | empresas.empresa_id  | CASCADE    | N        |
  | users.driver_id             | drivers.driver_id    | NO ACTION  | Y        |  ← see note 2
  | capacitaciones.driver_id    | drivers.driver_id    | CASCADE    | N        |
  | driver_positions.driver_id  | drivers.driver_id    | CASCADE    | N        |
  | driver_positions.visita_id  | visitas.visita_id    | SET NULL   | Y        |

  Note 1: CR text proposed `visitas.ruta_id CASCADE`. MSSQL refuses with
  error 1785 ("may cause cycles or multiple cascade paths") because
  dias→visitas (CASCADE) and dias→rutas (CASCADE) →visitas (CASCADE/SET NULL)
  both reach `visitas`. SET NULL is rejected for the same reason — MSSQL's
  check covers any referential action, not just CASCADE. We use NO ACTION,
  which means deleting a ruta with attached visitas raises a FK violation.
  The application's `delete_dia` (CR-021) explicitly deletes visitas
  before rutas, so this constraint only surfaces if external SQL bypasses
  the helper — which is exactly the integrity guarantee we want.

  Note 2: CR text proposed `users.driver_id SET NULL`. The existing FK
  `fk_users_empresa_id_empresas` is ON DELETE SET NULL, AND empresas→drivers
  is ON DELETE CASCADE (existing). SQL Server's "multiple cascade paths"
  check fires for SET NULL too: deleting an empresa would SET NULL on
  users.empresa_id directly and CASCADE-delete the empresa's drivers, which
  in turn would SET NULL users.driver_id — two write paths to the same
  users row. Promoting `users.driver_id → drivers` to NO ACTION breaks the
  conflict; the application is responsible for nulling driver_id before
  deleting a driver (and drivers are rarely deleted: they go `activo=0`).

Idempotency: Alembic tracks revisions. SQLite is intentionally a no-op for
this migration — FKs declared in ORM metadata are baked into CREATE TABLE
when tests bootstrap a fresh DB via `Base.metadata.create_all`.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.core.config import settings

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


# ----------------------------------------------------------------------------
# FK matrix (declarative). Each row:
#   constraint_name, child_table, child_col, parent_table, parent_col, on_delete
#
# Adjustments vs the CR text (see module docstring for rationale):
#   * visitas.ruta_id → SET NULL (was CASCADE) — breaks multi-cascade path.
#   * users.driver_id → NO ACTION (was SET NULL) — breaks multi-path with
#     existing fk_users_empresa_id_empresas SET NULL.
# ----------------------------------------------------------------------------
FKS: list[tuple[str, str, str, str, str, str]] = [
    ("fk_dias_empresa_id_empresas", "dias_operativos", "empresa_id", "empresas", "empresa_id", "NO ACTION"),
    ("fk_rutas_dia_id_dias", "rutas", "dia_id", "dias_operativos", "dia_id", "CASCADE"),
    ("fk_rutas_driver_id_drivers", "rutas", "driver_id", "drivers", "driver_id", "NO ACTION"),
    ("fk_rutas_vehicle_id_vehicles", "rutas", "vehicle_id", "vehicles", "vehicle_id", "NO ACTION"),
    ("fk_visitas_dia_id_dias", "visitas", "dia_id", "dias_operativos", "dia_id", "CASCADE"),
    ("fk_visitas_ruta_id_rutas", "visitas", "ruta_id", "rutas", "ruta_id", "NO ACTION"),
    ("fk_visitas_empresa_id_empresas", "visitas", "empresa_id", "empresas", "empresa_id", "NO ACTION"),
    ("fk_user_empresas_user_id_users", "user_empresas", "user_id", "users", "user_id", "CASCADE"),
    ("fk_user_empresas_empresa_id_empresas", "user_empresas", "empresa_id", "empresas", "empresa_id", "CASCADE"),
    ("fk_users_driver_id_drivers", "users", "driver_id", "drivers", "driver_id", "NO ACTION"),
    ("fk_capacitaciones_driver_id_drivers", "capacitaciones", "driver_id", "drivers", "driver_id", "CASCADE"),
    ("fk_driver_positions_driver_id_drivers", "driver_positions", "driver_id", "drivers", "driver_id", "CASCADE"),
    ("fk_driver_positions_visita_id_visitas", "driver_positions", "visita_id", "visitas", "visita_id", "SET NULL"),
]


# ----------------------------------------------------------------------------
# uq_drivers_rut_active — index that depends on drivers.rut. Created by 0010.
# Re-created with the same filter after the ALTER COLUMN.
# ----------------------------------------------------------------------------
DRIVERS_RUT_INDEX_NAME = "uq_drivers_rut_active"
DRIVERS_RUT_INDEX_FILTER = "rut IS NOT NULL AND activo = 1"


def _qual(table: str) -> str:
    """Schema-qualified table name (MSSQL only)."""
    if IS_MSSQL:
        return f"[{SCHEMA}].[{table}]"
    return f'"{table}"'


def _check_orphans(child: str, child_col: str, parent: str, parent_col: str) -> None:
    """Abort the migration if any child row has a value not present in parent."""
    sql = sa.text(
        f"SELECT COUNT(*) FROM {_qual(child)} c "
        f"WHERE c.{child_col} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {_qual(parent)} p WHERE p.{parent_col} = c.{child_col})"
    )
    bind = op.get_bind()
    count = bind.execute(sql).scalar() or 0
    if count > 0:
        raise RuntimeError(
            f"CR-021 FK pre-check failed: {child}.{child_col} has {count} orphan rows "
            f"(no matching {parent}.{parent_col}). Fix data before re-running migration."
        )


def _table_exists(name: str) -> bool:
    """Quick existence probe (MSSQL `sys.tables` or sqlite_master)."""
    bind = op.get_bind()
    if IS_MSSQL:
        sql = sa.text(
            "SELECT COUNT(*) FROM sys.tables t "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE s.name = :schema AND t.name = :name"
        )
        return (bind.execute(sql, {"schema": SCHEMA, "name": name}).scalar() or 0) > 0
    sql = sa.text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:name")
    return (bind.execute(sql, {"name": name}).scalar() or 0) > 0


def _column_exists(table: str, column: str) -> bool:
    """Quick column existence probe (defensive for FKs whose column might have
    been dropped in an earlier CR — e.g. users.driver_id, users.empresa_id)."""
    bind = op.get_bind()
    if IS_MSSQL:
        sql = sa.text(
            "SELECT COUNT(*) FROM sys.columns c "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE s.name = :schema AND t.name = :tname AND c.name = :cname"
        )
        return (bind.execute(sql, {"schema": SCHEMA, "tname": table, "cname": column}).scalar() or 0) > 0
    sql = sa.text(f'PRAGMA table_info("{table}")')
    rows = bind.execute(sql).fetchall()
    return any(r[1] == column for r in rows)


def _fk_exists(name: str) -> bool:
    bind = op.get_bind()
    if IS_MSSQL:
        sql = sa.text(
            "SELECT COUNT(*) FROM sys.foreign_keys WHERE name = :name"
        )
        return (bind.execute(sql, {"name": name}).scalar() or 0) > 0
    # SQLite — checking named FKs is hard; we skip and rely on idempotency
    # of Alembic revision tracking.
    return False


def _index_exists(table: str, name: str) -> bool:
    bind = op.get_bind()
    if IS_MSSQL:
        sql = sa.text(
            "SELECT COUNT(*) FROM sys.indexes i "
            "JOIN sys.tables t ON i.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE s.name = :schema AND t.name = :tname AND i.name = :iname"
        )
        return (bind.execute(sql, {"schema": SCHEMA, "tname": table, "iname": name}).scalar() or 0) > 0
    return False


# ----------------------------------------------------------------------------
# Upgrade
# ----------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. drivers.rut → varchar(20). ──
    if IS_MSSQL:
        # 1a. Pre-check: would any existing data be truncated?
        width_row = bind.execute(
            sa.text(
                f"SELECT ISNULL(MAX(LEN(rut)), 0) AS max_len, COUNT(*) AS n "
                f"FROM {_qual('drivers')} WHERE rut IS NOT NULL"
            )
        ).first()
        max_len = int(width_row.max_len or 0) if width_row else 0
        n_with_rut = int(width_row.n or 0) if width_row else 0
        if max_len > 20:
            raise RuntimeError(
                f"CR-021 abort: drivers.rut has {n_with_rut} rows with max length "
                f"{max_len} > 20. Narrowing would truncate. Backfill required."
            )

        # 1b. DROP dependent filtered UNIQUE index on rut (created by 0010).
        # ALTER COLUMN rejects the change if any index references the column,
        # so we drop first then re-create with the identical filter clause.
        if _index_exists("drivers", DRIVERS_RUT_INDEX_NAME):
            op.execute(
                f"DROP INDEX {DRIVERS_RUT_INDEX_NAME} ON {_qual('drivers')}"
            )

        # 1c. ALTER COLUMN.
        op.alter_column(
            "drivers",
            "rut",
            existing_type=sa.String(50),
            type_=sa.String(20),
            existing_nullable=True,
            schema=ref_schema,
        )

        # 1d. Re-create the filtered UNIQUE index — same name, same filter.
        op.execute(
            f"CREATE UNIQUE INDEX {DRIVERS_RUT_INDEX_NAME} "
            f"ON {_qual('drivers')} (rut) "
            f"WHERE {DRIVERS_RUT_INDEX_FILTER}"
        )
    # On SQLite ALTER COLUMN width is a no-op (no type widths), skip.

    # ── 2. Hard FKs. Probe orphans, then ADD CONSTRAINT. ──
    if IS_MSSQL:
        for name, child, child_col, parent, parent_col, on_delete in FKS:
            # Defensive: skip if the child column does not exist in this DB
            # (e.g. users.driver_id may have been dropped in a separate CR).
            if not _table_exists(child) or not _table_exists(parent):
                continue
            if not _column_exists(child, child_col):
                continue
            if _fk_exists(name):
                continue
            _check_orphans(child, child_col, parent, parent_col)
            try:
                op.execute(
                    f"ALTER TABLE {_qual(child)} "
                    f"ADD CONSTRAINT {name} FOREIGN KEY ({child_col}) "
                    f"REFERENCES {_qual(parent)} ({parent_col}) "
                    f"ON DELETE {on_delete}"
                )
            except Exception as e:
                # Re-raise with the FK identity for easy diagnosis. Without
                # this the migration framework swallows it inside a generic
                # ProgrammingError that omits which constraint failed.
                raise RuntimeError(
                    f"CR-021 ADD CONSTRAINT failed for {name} "
                    f"({child}.{child_col} -> {parent}.{parent_col} "
                    f"ON DELETE {on_delete}): {e}"
                ) from e
    else:
        # SQLite: FKs cannot be added to an existing table without rebuilding
        # it. Our test workflow recreates the DB from scratch via Alembic, so
        # the FKs declared in models metadata already exist when CREATE TABLE
        # runs. This branch is a no-op (intentional).
        pass


# ----------------------------------------------------------------------------
# Downgrade
# ----------------------------------------------------------------------------

def downgrade() -> None:
    if IS_MSSQL:
        # Drop FKs first (reverse order, for symmetry).
        for name, child, _child_col, _parent, _parent_col, _on_delete in reversed(FKS):
            if not _table_exists(child):
                continue
            if not _fk_exists(name):
                continue
            op.execute(f"ALTER TABLE {_qual(child)} DROP CONSTRAINT {name}")

        # Restore drivers.rut width to its pre-CR-021 value.
        if _index_exists("drivers", DRIVERS_RUT_INDEX_NAME):
            op.execute(f"DROP INDEX {DRIVERS_RUT_INDEX_NAME} ON {_qual('drivers')}")
        op.alter_column(
            "drivers",
            "rut",
            existing_type=sa.String(20),
            type_=sa.String(50),
            existing_nullable=True,
            schema=ref_schema,
        )
        op.execute(
            f"CREATE UNIQUE INDEX {DRIVERS_RUT_INDEX_NAME} "
            f"ON {_qual('drivers')} (rut) "
            f"WHERE {DRIVERS_RUT_INDEX_FILTER}"
        )

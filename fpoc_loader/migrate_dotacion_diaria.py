"""Driver/vehicle ownership + daily staffing availability.

Idempotent migration for both SQLite and SQL Server:
- Adds empresa_id to drivers and vehicles.
- Creates dotacion_diaria for day-level availability/assignment overrides.
- Backfills empresa_id from fpoc_simpli_visits when possible, with a stable
  round-robin fallback over active empresas.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv

for _p in (BACKEND / ".env", BACKEND.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from core.db import backend, get_conn  # noqa: E402


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _column_exists(cn, table: str, column: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"PRAGMA table_info({table})")
        return any(str(r[1]).lower() == column.lower() for r in cur.fetchall())
    schema, table_name = ("fpoc", table.replace("fpoc_", ""))
    cur.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?
        """,
        schema, table_name, column,
    )
    return cur.fetchone() is not None


def _add_column(cn, table: str, column: str, sqlite_ddl: str, mssql_ddl: str, quiet: bool) -> None:
    if _column_exists(cn, table, column):
        _log(f"[skip] {table}.{column}", quiet)
        return
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {sqlite_ddl}")
    else:
        cur.execute(f"ALTER TABLE fpoc.{table.replace('fpoc_', '')} ADD {mssql_ddl}")
    cn.commit()
    _log(f"[ok]   {table}.{column}", quiet)


def _ensure_dotacion_table(cn, quiet: bool) -> None:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fpoc_dotacion_diaria (
                dotacion_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha              DATE     NOT NULL,
                empresa_id         INTEGER  NOT NULL,
                driver_id          TEXT,
                vehicle_id         INTEGER,
                estado             TEXT     NOT NULL DEFAULT 'disponible',
                motivo             TEXT,
                updated_by_user_id INTEGER,
                created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id),
                FOREIGN KEY (driver_id) REFERENCES fpoc_drivers(driver_id),
                FOREIGN KEY (vehicle_id) REFERENCES fpoc_vehicles(vehicle_id)
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS UX_dotacion_diaria_driver
            ON fpoc_dotacion_diaria(fecha, empresa_id, driver_id)
            WHERE driver_id IS NOT NULL
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS UX_dotacion_diaria_vehicle
            ON fpoc_dotacion_diaria(fecha, empresa_id, vehicle_id)
            WHERE vehicle_id IS NOT NULL
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS IX_dotacion_diaria_scope
            ON fpoc_dotacion_diaria(fecha, empresa_id, estado)
            """
        )
    else:
        cur.execute(
            """
            IF OBJECT_ID('fpoc.dotacion_diaria', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.dotacion_diaria (
                    dotacion_id        INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    fecha              DATE          NOT NULL,
                    empresa_id         INT           NOT NULL,
                    driver_id          NVARCHAR(20)  NULL,
                    vehicle_id         INT           NULL,
                    estado             NVARCHAR(30)  NOT NULL DEFAULT 'disponible',
                    motivo             NVARCHAR(500) NULL,
                    updated_by_user_id INT           NULL,
                    created_at         DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
                    updated_at         DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
                );
            END
            """
        )
        cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_dotacion_diaria_driver')
                CREATE UNIQUE INDEX UX_dotacion_diaria_driver
                ON fpoc.dotacion_diaria(fecha, empresa_id, driver_id)
                WHERE driver_id IS NOT NULL
            """
        )
        cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_dotacion_diaria_vehicle')
                CREATE UNIQUE INDEX UX_dotacion_diaria_vehicle
                ON fpoc.dotacion_diaria(fecha, empresa_id, vehicle_id)
                WHERE vehicle_id IS NOT NULL
            """
        )
        cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_dotacion_diaria_scope')
                CREATE INDEX IX_dotacion_diaria_scope
                ON fpoc.dotacion_diaria(fecha, empresa_id, estado)
            """
        )
    cn.commit()
    _log("[ok]   dotacion_diaria", quiet)


def _fetch_empresas(cn) -> list[int]:
    cur = cn.cursor()
    try:
        cur.execute("SELECT empresa_id FROM fpoc.empresas_transporte WHERE activo = 1 ORDER BY empresa_id")
    except Exception:
        return []
    return [int(r[0]) for r in cur.fetchall()]


def _vehicle_empresa_from_visits(cn) -> dict[int, int]:
    cur = cn.cursor()
    try:
        cur.execute(
            """
            SELECT patente_falsa AS vehicle_id, empresa_falsa AS empresa_id, COUNT(*) AS n
            FROM fpoc.simpli_visits
            WHERE patente_falsa IS NOT NULL AND empresa_falsa IS NOT NULL
            GROUP BY patente_falsa, empresa_falsa
            """
        )
    except Exception:
        return {}
    best: dict[int, tuple[int, int]] = {}
    for r in cur.fetchall():
        vid = int(r.vehicle_id)
        eid = int(r.empresa_id)
        n = int(r.n or 0)
        if vid not in best or n > best[vid][1]:
            best[vid] = (eid, n)
    return {vid: eid for vid, (eid, _) in best.items()}


def _backfill_empresas(cn, quiet: bool) -> None:
    empresas = _fetch_empresas(cn)
    if not empresas:
        return
    inferred = _vehicle_empresa_from_visits(cn)
    cur = cn.cursor()

    cur.execute("SELECT vehicle_id, empresa_id FROM fpoc.vehicles ORDER BY vehicle_id")
    vehicles = cur.fetchall()
    updated_vehicles = 0
    vehicle_to_empresa: dict[int, int] = {}
    for i, v in enumerate(vehicles):
        vid = int(v.vehicle_id)
        eid = int(v.empresa_id) if getattr(v, "empresa_id", None) is not None else None
        if eid is None:
            eid = inferred.get(vid) or empresas[i % len(empresas)]
            cur.execute("UPDATE fpoc.vehicles SET empresa_id = ? WHERE vehicle_id = ?", eid, vid)
            updated_vehicles += 1
        vehicle_to_empresa[vid] = eid

    cur.execute("SELECT driver_id, vehicle_id, empresa_id FROM fpoc.drivers ORDER BY driver_id")
    drivers = cur.fetchall()
    updated_drivers = 0
    for i, d in enumerate(drivers):
        eid = int(d.empresa_id) if getattr(d, "empresa_id", None) is not None else None
        if eid is not None:
            continue
        vid = int(d.vehicle_id) if d.vehicle_id is not None else None
        eid = vehicle_to_empresa.get(vid) if vid is not None else None
        if eid is None:
            eid = empresas[i % len(empresas)]
        cur.execute("UPDATE fpoc.drivers SET empresa_id = ? WHERE driver_id = ?", eid, d.driver_id)
        updated_drivers += 1

    cn.commit()
    _log(f"[backfill] vehicles={updated_vehicles} drivers={updated_drivers}", quiet)


def main(*, quiet: bool = False) -> int:
    _log(f"[migrate-dotacion] backend={backend()}", quiet)
    with get_conn() as cn:
        _add_column(cn, "fpoc_drivers", "empresa_id", "empresa_id INTEGER", "empresa_id INT NULL", quiet)
        _add_column(cn, "fpoc_vehicles", "empresa_id", "empresa_id INTEGER", "empresa_id INT NULL", quiet)
        _ensure_dotacion_table(cn, quiet)
        _backfill_empresas(cn, quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())

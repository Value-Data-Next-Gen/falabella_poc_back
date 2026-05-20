"""Agrega columnas activation_token / activation_used_at a 3 tablas.

Tablas afectadas:
  - fpoc_users
  - fpoc_drivers
  - fpoc_empresa_contactos

Cada una recibe:
  - activation_token   VARCHAR(32) NULL  (TEXT en sqlite)
  - activation_used_at DATETIME2(0) NULL (TEXT/TIMESTAMP en sqlite)

Idempotente en SQLite + SQL Server: chequea INFORMATION_SCHEMA / PRAGMA antes
de ALTER TABLE.

Uso manual:
    python backend/fpoc_loader/migrate_activation_tokens.py

Corre automáticamente en lifespan via fpoc_loader.migrations.MIGRATIONS bajo
el id "025_activation_tokens".
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


TABLES = (
    ("fpoc_users", "users"),
    ("fpoc_drivers", "drivers"),
    ("fpoc_empresa_contactos", "empresa_contactos"),
)


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _table_exists(cn, sqlite_table: str, mssql_table: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (sqlite_table,),
        )
        return cur.fetchone() is not None
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = ?
        """,
        mssql_table,
    )
    return cur.fetchone() is not None


def _column_exists(cn, sqlite_table: str, mssql_table: str, column: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"PRAGMA table_info({sqlite_table})")
        return any(str(r[1]).lower() == column.lower() for r in cur.fetchall())
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = ? AND COLUMN_NAME = ?
        """,
        mssql_table, column,
    )
    return cur.fetchone() is not None


def _add_column(cn, sqlite_table: str, mssql_table: str, column: str,
                sqlite_type: str, mssql_type: str, quiet: bool) -> None:
    # Si la tabla no existe (DB recién creada sin bootstrap), nos saltamos
    # silenciosamente — la próxima corrida tras bootstrap aplicará la columna.
    if not _table_exists(cn, sqlite_table, mssql_table):
        _log(f"[skip] {sqlite_table} no existe (¿bootstrap pendiente?)", quiet)
        return
    if _column_exists(cn, sqlite_table, mssql_table, column):
        _log(f"[skip] {sqlite_table}.{column} ya existe", quiet)
        return
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"ALTER TABLE {sqlite_table} ADD COLUMN {column} {sqlite_type}")
    else:
        cur.execute(f"ALTER TABLE fpoc.{mssql_table} ADD {column} {mssql_type} NULL")
    cn.commit()
    _log(f"[ok]   {sqlite_table}.{column} agregada", quiet)


def main(quiet: bool = False) -> None:
    _log(f"[migrate-activation-tokens] backend={backend()}", quiet)
    with get_conn() as cn:
        for sqlite_table, mssql_table in TABLES:
            _add_column(
                cn, sqlite_table, mssql_table,
                column="activation_token",
                sqlite_type="TEXT",
                mssql_type="VARCHAR(32)",
                quiet=quiet,
            )
            _add_column(
                cn, sqlite_table, mssql_table,
                column="activation_used_at",
                sqlite_type="TIMESTAMP",
                mssql_type="DATETIME2(0)",
                quiet=quiet,
            )


if __name__ == "__main__":
    main()

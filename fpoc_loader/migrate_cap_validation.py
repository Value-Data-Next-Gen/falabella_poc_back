"""Agrega validated_by_user_id + validated_at a driver_capacitaciones.

Permite que Falabella (admin/ops) marque un registro de capacitación como
"validado" — útil cuando el transport_manager carga el certificado pero
Falabella lo confirma. Si validated_at es NULL, el registro está "pendiente
de validación".

Idempotente sqlite/sqlserver.
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
    cur.execute(
        """SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = ? AND COLUMN_NAME = ?""",
        table.replace("fpoc_", ""), column,
    )
    return cur.fetchone() is not None


def _add_col(cn, column: str, sqlite_ddl: str, mssql_ddl: str, quiet: bool) -> None:
    if _column_exists(cn, "fpoc_driver_capacitaciones", column):
        _log(f"[skip] driver_capacitaciones.{column}", quiet)
        return
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"ALTER TABLE fpoc_driver_capacitaciones ADD COLUMN {sqlite_ddl}")
    else:
        cur.execute(f"ALTER TABLE fpoc.driver_capacitaciones ADD {mssql_ddl}")
    cn.commit()
    _log(f"[ok]   driver_capacitaciones.{column}", quiet)


def main(quiet: bool = False) -> None:
    _log(f"[migrate-cap-validation] backend={backend()}", quiet)
    with get_conn() as cn:
        _add_col(cn, "validated_by_user_id", "validated_by_user_id INTEGER",
                  "validated_by_user_id INT NULL", quiet)
        _add_col(cn, "validated_at", "validated_at TIMESTAMP",
                  "validated_at DATETIME2(0) NULL", quiet)


if __name__ == "__main__":
    main()

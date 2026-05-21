"""Empresa: agrega central_phone (teléfono central/despachador de la empresa).

Un solo número por empresa. Si llega un mensaje WhatsApp desde este número,
se trata como el dispatcher/jefe central que puede broadcast a sus drivers.

Idempotente. Azure SQL único backend.
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

from core.db import get_conn  # noqa: E402


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _column_exists(cn, table: str, column: str) -> bool:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = ? AND COLUMN_NAME = ?
        """,
        table.replace("fpoc_", ""), column,
    )
    return cur.fetchone() is not None


def _add_central_phone(cn, quiet: bool) -> None:
    if _column_exists(cn, "fpoc_empresas_transporte", "central_phone"):
        _log("[skip] empresas_transporte.central_phone ya existe", quiet)
        return
    cur = cn.cursor()
    cur.execute("ALTER TABLE fpoc.empresas_transporte ADD central_phone NVARCHAR(20) NULL")
    cn.commit()
    _log("[ok]   empresas_transporte.central_phone agregado", quiet)


def main(quiet: bool = False) -> None:
    _log("[migrate-empresa-central] backend=sqlserver", quiet)
    with get_conn() as cn:
        _add_central_phone(cn, quiet)


if __name__ == "__main__":
    main()

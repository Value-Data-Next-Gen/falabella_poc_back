"""Agrega rol 'driver' al check constraint de users + columna driver_id (FK).

Permite que cada driver de fpoc.drivers tenga (opcionalmente) una cuenta de
login asociada con role='driver'. El driver loguea y solo ve sus pedidos +
puede subir su documentación.

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


def _column_exists_users(cn, column: str) -> bool:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = 'users' AND COLUMN_NAME = ?
        """,
        column,
    )
    return cur.fetchone() is not None


def _add_driver_id_column(cn, quiet: bool) -> None:
    if _column_exists_users(cn, "driver_id"):
        _log("[skip] users.driver_id ya existe", quiet)
        return
    cur = cn.cursor()
    cur.execute("ALTER TABLE fpoc.users ADD driver_id NVARCHAR(20) NULL")
    cur.execute(
        """
        IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_users_driver')
            CREATE INDEX IX_users_driver ON fpoc.users(driver_id)
        """
    )
    cn.commit()
    _log("[ok]   users.driver_id agregado", quiet)


def _update_role_check(cn, quiet: bool) -> None:
    """Drop + recreate CK_users_role para incluir 'driver'."""
    cur = cn.cursor()
    cur.execute(
        """
        IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_users_role')
            ALTER TABLE fpoc.users DROP CONSTRAINT CK_users_role
        """
    )
    cur.execute(
        """
        ALTER TABLE fpoc.users
        ADD CONSTRAINT CK_users_role
        CHECK (role IN ('falabella_admin', 'falabella_ops', 'transport_manager', 'driver'))
        """
    )
    cn.commit()
    _log("[ok]   CK_users_role actualizado con 'driver'", quiet)


def main(quiet: bool = False) -> None:
    _log("[migrate-driver-role] backend=sqlserver", quiet)
    with get_conn() as cn:
        _add_driver_id_column(cn, quiet)
        _update_role_check(cn, quiet)


if __name__ == "__main__":
    main()

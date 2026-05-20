"""Agrega rol 'driver' al check constraint de users + columna driver_id (FK).

Permite que cada driver de fpoc.drivers tenga (opcionalmente) una cuenta de
login asociada con role='driver'. El driver loguea y solo ve sus pedidos +
puede subir su documentación.

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


def _column_exists_users(cn, column: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute("PRAGMA table_info(fpoc_users)")
        return any(str(r[1]).lower() == column.lower() for r in cur.fetchall())
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
    if backend() == "sqlite":
        cur.execute("ALTER TABLE fpoc_users ADD COLUMN driver_id TEXT")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS IX_users_driver ON fpoc_users(driver_id)"
        )
    else:
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
    """En sqlite el CHECK incluido en CREATE TABLE no permite rol 'driver'.
    Estrategia: drop + recreate constraint via copy table (sqlite no soporta
    ALTER CHECK directo). En sqlserver, drop y recrear el constraint.
    """
    cur = cn.cursor()
    if backend() == "sqlite":
        # Verificar si el CHECK actual ya soporta 'driver'
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fpoc_users'")
        row = cur.fetchone()
        if row and "'driver'" in (row[0] or ""):
            _log("[skip] sqlite check role ya incluye 'driver'", quiet)
            return
        # Recrear tabla con check ampliado
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute(
            """
            CREATE TABLE fpoc_users_new (
                user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                email          TEXT      NOT NULL UNIQUE,
                password_hash  TEXT      NOT NULL,
                display_name   TEXT      NOT NULL,
                role           TEXT      NOT NULL CHECK (role IN ('falabella_admin', 'falabella_ops', 'transport_manager', 'driver')),
                empresa_id     INTEGER,
                driver_id      TEXT,
                activo         INTEGER   NOT NULL DEFAULT 1,
                created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login     TIMESTAMP,
                FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id),
                FOREIGN KEY (driver_id)  REFERENCES fpoc_drivers(driver_id)
            )
            """
        )
        cur.execute(
            """INSERT INTO fpoc_users_new (user_id, email, password_hash, display_name,
                                            role, empresa_id, activo, created_at, last_login)
               SELECT user_id, email, password_hash, display_name, role, empresa_id,
                      activo, created_at, last_login FROM fpoc_users"""
        )
        cur.execute("DROP TABLE fpoc_users")
        cur.execute("ALTER TABLE fpoc_users_new RENAME TO fpoc_users")
        cur.execute("CREATE INDEX IF NOT EXISTS IX_users_empresa ON fpoc_users(empresa_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS IX_users_role ON fpoc_users(role)")
        cur.execute("CREATE INDEX IF NOT EXISTS IX_users_driver ON fpoc_users(driver_id)")
        cur.execute("PRAGMA foreign_keys=ON")
        cn.commit()
        _log("[ok]   sqlite check role recreado con 'driver'", quiet)
    else:
        # SQL Server: drop CK_users_role si existe, recrear con driver incluido
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
    _log(f"[migrate-driver-role] backend={backend()}", quiet)
    with get_conn() as cn:
        _add_driver_id_column(cn, quiet)
        _update_role_check(cn, quiet)


if __name__ == "__main__":
    main()

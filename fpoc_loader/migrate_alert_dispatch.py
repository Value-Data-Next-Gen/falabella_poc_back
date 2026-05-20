"""CR-012 T0.3 — Alert dispatch log + supervisor_phone_e164 en empresas.

Agrega a `fpoc_empresas_transporte`:
  - supervisor_phone_e164  TEXT  (E.164, NULL = sin configurar → mock en API)

Crea `fpoc_alert_dispatch_log` (idempotente):
  - alert_id          PK
  - tracking_id       TEXT NOT NULL (indexado)
  - type              'retraso_vip' | 'driver_sin_respuesta' | 'motivo_patron'
  - channel           'whatsapp' | 'sms' | 'in_app'
  - target            'cliente' | 'driver' | 'supervisor'
  - sent_at           TIMESTAMP
  - acknowledged_at   TIMESTAMP NULL
  - ruta_id           TEXT NULL
  - empresa_id        INTEGER NULL
  - payload_json      TEXT NULL (rendered template, debug)
  - created_by_user_id INTEGER NULL

Reusa el patrón _column_exists / _add_column del sprint 4.A1.

Uso:
    python valuedata_backend/fpoc_loader/migrate_alert_dispatch.py
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


def _column_exists(cn, table: str, column: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"PRAGMA table_info({table})")
        rows = cur.fetchall()
        return any(r[1].lower() == column.lower() for r in rows)
    cur.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? AND COLUMN_NAME = ?",
        table.replace("fpoc_", ""), column,
    )
    return cur.fetchone() is not None


def _table_exists(cn, table: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            table,
        )
        return cur.fetchone() is not None
    cur.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?",
        table.replace("fpoc_", ""),
    )
    return cur.fetchone() is not None


def _add_column(cn, table: str, column: str, sqlite_ddl: str, sqlserver_ddl: str) -> bool:
    """Bifurcado: SQLite usa `ADD COLUMN <name> TEXT`; SQL Server usa
    `ADD <name> NVARCHAR(N)` (sin la palabra COLUMN, tipos NVARCHAR)."""
    if _column_exists(cn, table, column):
        print(f"[skip] {table}.{column} ya existe")
        return False
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {sqlite_ddl}")
    else:
        cur.execute(f"ALTER TABLE {table} ADD {sqlserver_ddl}")
    cn.commit()
    print(f"[ok]   {table}.{column} agregado")
    return True


def _create_alert_dispatch_log(cn) -> None:
    if _table_exists(cn, "fpoc_alert_dispatch_log"):
        print("[skip] fpoc_alert_dispatch_log ya existe")
        return
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.executescript("""
            CREATE TABLE fpoc_alert_dispatch_log (
                alert_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id         TEXT NOT NULL,
                type                TEXT NOT NULL CHECK (type IN ('retraso_vip','driver_sin_respuesta','motivo_patron')),
                channel             TEXT NOT NULL CHECK (channel IN ('whatsapp','sms','in_app')),
                target              TEXT NOT NULL CHECK (target IN ('cliente','driver','supervisor')),
                sent_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at     TIMESTAMP,
                ruta_id             TEXT,
                empresa_id          INTEGER,
                payload_json        TEXT,
                created_by_user_id  INTEGER
            );
            CREATE INDEX IX_alert_dispatch_tracking ON fpoc_alert_dispatch_log(tracking_id);
            CREATE INDEX IX_alert_dispatch_sent     ON fpoc_alert_dispatch_log(sent_at);
            CREATE INDEX IX_alert_dispatch_empresa  ON fpoc_alert_dispatch_log(empresa_id);
        """)
    else:
        cur.execute("""
            CREATE TABLE fpoc.alert_dispatch_log (
                alert_id            INT IDENTITY(1,1) PRIMARY KEY,
                tracking_id         NVARCHAR(80) NOT NULL,
                type                NVARCHAR(40) NOT NULL CHECK (type IN ('retraso_vip','driver_sin_respuesta','motivo_patron')),
                channel             NVARCHAR(20) NOT NULL CHECK (channel IN ('whatsapp','sms','in_app')),
                target              NVARCHAR(20) NOT NULL CHECK (target IN ('cliente','driver','supervisor')),
                sent_at             DATETIME2(0) NOT NULL DEFAULT SYSDATETIME(),
                acknowledged_at     DATETIME2(0) NULL,
                ruta_id             NVARCHAR(60) NULL,
                empresa_id          INT NULL,
                payload_json        NVARCHAR(MAX) NULL,
                created_by_user_id  INT NULL
            );
        """)
        cur.execute("CREATE INDEX IX_alert_dispatch_tracking ON fpoc.alert_dispatch_log(tracking_id);")
        cur.execute("CREATE INDEX IX_alert_dispatch_sent     ON fpoc.alert_dispatch_log(sent_at);")
        cur.execute("CREATE INDEX IX_alert_dispatch_empresa  ON fpoc.alert_dispatch_log(empresa_id);")
    cn.commit()
    print("[ok]   fpoc_alert_dispatch_log creada con 3 índices")


def main(quiet: bool = False) -> int:
    if not quiet:
        print(f"[migrate] backend={backend()}")
    with get_conn() as cn:
        _add_column(
            cn,
            "fpoc_empresas_transporte",
            "supervisor_phone_e164",
            sqlite_ddl="supervisor_phone_e164 TEXT",
            sqlserver_ddl="supervisor_phone_e164 NVARCHAR(20) NULL",
        )
        _create_alert_dispatch_log(cn)
    return 0


if __name__ == "__main__":
    sys.exit(main())

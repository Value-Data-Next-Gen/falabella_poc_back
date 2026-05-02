"""Migración: agrega columnas deadline a fpoc_vip_clients.

Agrega:
  - deadline_time TEXT (HH:MM, NULL si no aplica)
  - alert_minutes_before INTEGER NOT NULL DEFAULT 60
  - last_alert_sent_at TIMESTAMP

Idempotente: chequea PRAGMA table_info antes de ALTER TABLE.

Uso:
    python valuedata_backend/fpoc_loader/migrate_vip_deadline.py
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

from db import backend, get_conn  # noqa: E402


COLUMNS_TO_ADD = [
    ("deadline_time", "TEXT"),
    ("alert_minutes_before", "INTEGER NOT NULL DEFAULT 60"),
    ("last_alert_sent_at", "TIMESTAMP"),
]


def _column_exists(cn, table: str, column: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"PRAGMA table_info({table})")
        rows = cur.fetchall()
        return any(r[1].lower() == column.lower() for r in rows)
    # SQL Server
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = ? AND COLUMN_NAME = ?
        """,
        table.replace("fpoc_", ""), column,
    )
    return cur.fetchone() is not None


def main() -> int:
    print(f"[migrate-vip-deadline] backend={backend()}")
    with get_conn() as cn:
        cur = cn.cursor()
        for col_name, col_def in COLUMNS_TO_ADD:
            if _column_exists(cn, "fpoc_vip_clients", col_name):
                print(f"[skip] fpoc_vip_clients.{col_name} ya existe")
                continue
            sql = f"ALTER TABLE fpoc_vip_clients ADD COLUMN {col_name} {col_def}"
            cur.execute(sql)
            print(f"[ok] fpoc_vip_clients.{col_name} agregado")
        cn.commit()

        # Resumen
        cur.execute("SELECT COUNT(*) AS n FROM fpoc_vip_clients WHERE deadline_time IS NOT NULL")
        n_with = int(cur.fetchone().n)
        cur.execute("SELECT COUNT(*) AS n FROM fpoc_vip_clients")
        n_total = int(cur.fetchone().n)
        print(f"[summary] fpoc_vip_clients: {n_total} filas, {n_with} con deadline_time")
    return 0


if __name__ == "__main__":
    sys.exit(main())

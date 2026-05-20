"""Aplica una migración SQL (archivo ddl con GO como separador de batches).

Uso:
    python fpoc_loader/apply_migration.py notifications_ddl.sql
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def get_conn() -> pyodbc.Connection:
    for p in (HERE.parent / ".env", HERE.parent.parent / ".env"):
        if p.exists():
            load_dotenv(p)
            break
    conn_str = (
        f"DRIVER={{{os.environ['DB_DRIVER']}}};"
        f"SERVER={os.environ['DB_SERVER'].replace('tcp:', '')};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, autocommit=False)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Uso: python apply_migration.py <archivo.sql>")
        return 1
    name = argv[1]
    path = (HERE / name) if not Path(name).is_absolute() else Path(name)
    if not path.exists():
        print(f"No existe: {path}")
        return 1
    sql = path.read_text(encoding="utf-8")
    batches = [b.strip() for b in sql.split("\nGO\n") if b.strip()]
    with get_conn() as cn:
        cur = cn.cursor()
        for i, b in enumerate(batches, 1):
            try:
                cur.execute(b)
                cn.commit()
                print(f"[{i}/{len(batches)}] OK")
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(batches)}] FALLO: {e}")
                cn.rollback()
                return 2
    print(f"[done] {path.name} aplicado")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

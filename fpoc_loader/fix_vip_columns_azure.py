"""One-shot: agrega columnas deadline a fpoc.vip_clients en Azure SQL.

T-SQL no usa `ADD COLUMN` (solo `ADD`), `TEXT` esta deprecado, y `TIMESTAMP`
es un tipo de versionado, no fecha. Usamos NVARCHAR / DATETIME2.

Idempotente: chequea INFORMATION_SCHEMA.COLUMNS antes de cada ALTER.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
for _p in (BACKEND / ".env", BACKEND.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break


COLUMNS = [
    ("deadline_time", "NVARCHAR(8) NULL"),
    ("alert_minutes_before", "INT NOT NULL CONSTRAINT DF_vip_alert_min DEFAULT 60 WITH VALUES"),
    ("last_alert_sent_at", "DATETIME2 NULL"),
]


def main() -> int:
    import pyodbc
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    cs = (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    schema = os.environ.get("DB_SCHEMA", "fpoc")
    table = "vip_clients"
    cn = pyodbc.connect(cs, autocommit=True)
    cur = cn.cursor()
    print(f"[fix-vip] schema={schema} table={table}")
    for col, ddl in COLUMNS:
        cur.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?",
            schema, table, col,
        )
        if cur.fetchone():
            print(f"  [skip] {col} ya existe")
            continue
        sql = f"ALTER TABLE [{schema}].[{table}] ADD {col} {ddl}"
        print(f"  [run] {sql}")
        cur.execute(sql)
        print(f"  [ok]  {col} agregada")
    cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
    n = int(cur.fetchone()[0])
    print(f"[summary] {schema}.{table}: {n} filas")
    cn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

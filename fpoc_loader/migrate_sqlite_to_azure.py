"""Migra datos de SQLite local (valuedata.db) a Azure SQL (schema fpoc).

Pre-requisitos:
  - .env con DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD, DB_DRIVER, DB_SCHEMA=fpoc
  - Firewall del SQL Server abierto para tu IP actual
  - Schema fpoc.* creado en Azure SQL (corre primero ddl.sql + users_ddl.sql +
    notifications_ddl.sql + access_log_ddl.sql + content_templates_ddl.sql)

Uso:
    cd backend
    python fpoc_loader/migrate_sqlite_to_azure.py [--tables tab1,tab2] [--dry]

Estrategia por tabla:
  1. SELECT COUNT(*) en Azure: si > 0, skip a menos que --truncate
  2. SELECT * de SQLite local
  3. Mapear tipos (SQLite→pyodbc params)
  4. INSERT batched (chunk 500) con executemany
"""
from __future__ import annotations

import argparse
import io
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Forzar stdout UTF-8 para emojis/flechas (Windows cp1252).
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
WS_ROOT = BACKEND.parent

# Cargar .env del workspace o backend
for _p in (BACKEND / ".env", WS_ROOT / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

SQLITE_PATH = os.environ.get("SQLITE_PATH", str(BACKEND / "valuedata.db"))


# Orden de migración (respeta FKs)
TABLES_ORDER = [
    "fpoc_empresas_transporte",
    "fpoc_users",
    "fpoc_drivers",
    "fpoc_vehicles",
    "fpoc_clients",
    "fpoc_simpli_visits",
    "fpoc_geo_suborders",
    "fpoc_motivo_alert_config",
    "fpoc_vip_clients",
    "fpoc_visit_priority_overrides",
    "fpoc_visit_comments",
    "fpoc_motivo_corrections",
    "fpoc_empresa_contactos",
    "fpoc_notifications_log",
    "fpoc_planificacion_imports",
    "fpoc_whatsapp_sessions",
    "fpoc_app_config",
    "fpoc_access_log",
]


def open_azure():
    import pyodbc
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    cs = (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(cs, autocommit=False)


def open_sqlite():
    if not Path(SQLITE_PATH).exists():
        raise FileNotFoundError(f"SQLite no encontrado en {SQLITE_PATH}")
    cn = sqlite3.connect(SQLITE_PATH)
    cn.row_factory = sqlite3.Row
    return cn


def normalize_value(v):
    """Convierte tipos SQLite a tipos compatibles con pyodbc."""
    if v is None:
        return None
    if isinstance(v, (int, float, str, bytes)):
        return v
    return str(v)


def get_columns_sqlite(cn, table: str) -> list[str]:
    cur = cn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def get_columns_azure(cn_az, schema: str, table_no_prefix: str) -> list[str]:
    """table_no_prefix: 'simpli_visits' (sin 'fpoc_')."""
    cur = cn_az.cursor()
    cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        schema, table_no_prefix,
    )
    return [r[0] for r in cur.fetchall()]


def migrate_table(sqlite_cn, az_cn, table: str, dry: bool, truncate: bool, batch: int):
    schema = os.environ.get("DB_SCHEMA", "fpoc")
    table_no_prefix = table.replace("fpoc_", "", 1)
    az_table = f"[{schema}].[{table_no_prefix}]"

    sl_cols = get_columns_sqlite(sqlite_cn, table)
    if not sl_cols:
        print(f"  [skip] {table}: tabla no existe en SQLite")
        return 0
    az_cols = get_columns_azure(az_cn, schema, table_no_prefix)
    if not az_cols:
        print(f"  [skip] {table}: tabla no existe en Azure SQL ({az_table}) — correr DDL primero")
        return 0
    common = [c for c in sl_cols if c in az_cols]
    if not common:
        print(f"  [skip] {table}: sin columnas en común entre SQLite y Azure")
        return 0
    missing_az = [c for c in sl_cols if c not in az_cols]
    if missing_az:
        print(f"  [warn] {table}: columnas en SQLite que NO están en Azure (se ignoran): {missing_az}")

    az_cur = az_cn.cursor()
    az_cur.execute(f"SELECT COUNT(*) FROM {az_table}")
    az_count = int(az_cur.fetchone()[0])
    sl_count = sqlite_cn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"  {table}: SQLite={sl_count} | Azure={az_count}")

    if az_count > 0 and not truncate:
        print(f"    skip (Azure ya tiene datos; usar --truncate para sobrescribir)")
        return 0
    if az_count > 0 and truncate:
        if dry:
            print(f"    [dry] DELETE FROM {az_table}")
        else:
            az_cur.execute(f"DELETE FROM {az_table}")
            az_cn.commit()
            print(f"    [truncate] Azure: {az_count} filas borradas")

    if sl_count == 0:
        return 0

    cols_sql = ", ".join(f"[{c}]" for c in common)
    placeholders = ", ".join(["?"] * len(common))
    insert_sql = f"INSERT INTO {az_table} ({cols_sql}) VALUES ({placeholders})"

    sl_cur = sqlite_cn.execute(f"SELECT {', '.join(common)} FROM {table}")
    inserted = 0
    chunk: list[tuple] = []
    for row in sl_cur:
        vals = tuple(normalize_value(row[c]) for c in common)
        chunk.append(vals)
        if len(chunk) >= batch:
            if dry:
                inserted += len(chunk)
            else:
                try:
                    az_cur.fast_executemany = True
                except Exception:
                    pass
                az_cur.executemany(insert_sql, chunk)
                az_cn.commit()
                inserted += len(chunk)
            chunk = []
    if chunk:
        if dry:
            inserted += len(chunk)
        else:
            try:
                az_cur.fast_executemany = True
            except Exception:
                pass
            az_cur.executemany(insert_sql, chunk)
            az_cn.commit()
            inserted += len(chunk)
    print(f"    [{'DRY' if dry else 'OK'}] {inserted} filas insertadas en {az_table}")
    return inserted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", help="lista separada por comas; default: todas en TABLES_ORDER")
    ap.add_argument("--truncate", action="store_true", help="borrar Azure antes de insertar")
    ap.add_argument("--dry", action="store_true", help="no escribe; solo cuenta")
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()

    tables = TABLES_ORDER
    if args.tables:
        wanted = [t.strip() for t in args.tables.split(",") if t.strip()]
        tables = [t for t in wanted if t]

    print(f"=== Migración SQLite → Azure SQL ===")
    print(f"  SQLite: {SQLITE_PATH}")
    print(f"  Azure:  {os.environ['DB_SERVER']}/{os.environ['DB_NAME']} schema={os.environ.get('DB_SCHEMA', 'fpoc')}")
    print(f"  Modo: {'DRY-RUN' if args.dry else 'WRITE'}{' + TRUNCATE' if args.truncate else ''}")
    print(f"  Tablas: {len(tables)}")
    print()

    sl = open_sqlite()
    az = open_azure()
    print("Conexión OK")
    print()

    total = 0
    t0 = time.time()
    for t in tables:
        try:
            n = migrate_table(sl, az, t, args.dry, args.truncate, args.batch)
            total += n
        except Exception as e:
            print(f"  [ERR] {t}: {e}")

    sl.close()
    az.close()
    elapsed = time.time() - t0
    print()
    print(f"=== Total: {total} filas migradas en {elapsed:.1f}s ===")


if __name__ == "__main__":
    main()

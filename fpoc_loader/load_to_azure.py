"""Carga datos_eta_YYYY-MM-DD.xlsx a Azure SQL (schema fpoc).

Idempotente: aplica DDL si falta, hace TRUNCATE+INSERT por fecha del archivo.
Credenciales desde .env (DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD, DB_DRIVER).

Uso:
    python fpoc_loader/load_to_azure.py                                   # busca datos_eta_*.xlsx en el root
    python fpoc_loader/load_to_azure.py datos_eta_2026_04_19.xlsx          # ruta explícita
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pyodbc
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
DDL_PATH = HERE / "ddl.sql"
# Buscar el xlsx en backend/, backend/../ y cwd.
SEARCH_DIRS = [BACKEND_ROOT, BACKEND_ROOT.parent, Path.cwd()]

SIMPLI_COLS = [
    "planned_date", "id", "title", "order", "address",
    "checkout_cl", "current_eta_cl", "status",
    "checkout_comment", "checkout_observation", "reference", "country",
    "sla_hour_checkout_eta", "bin_start", "bin_end", "bin_label", "bin_index",
    "ct", "Patente_falsa", "Empresa_falsa", "Drivername",
    "Fechainicioruta", "Fechainicioruta_hora_cl",
    "fechas_futuras_bq", "finicio_currenteta_bq",
    "current_eta_cl_fechainicioruta", "current_eta_cl_fechainicioruta_dates",
    "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
    "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
    "am_pm", "ruta_anomala",
]

GEO_COLS = [
    "Suborden", "fechainicioruta", "patente_falsa", "empresa_falsa",
    "idruta", "do", "lpn", "parentorder",
    "direccion", "localidad", "region", "fechapactada",
    "tipodocumento", "estado", "motivonoentrega", "comentarionoentrega",
]


def get_conn() -> pyodbc.Connection:
    load_dotenv(ROOT / ".env")
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    conn_str = (
        f"DRIVER={{{os.environ['DB_DRIVER']}}};"
        f"SERVER={server};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, autocommit=False)


def apply_ddl(cn: pyodbc.Connection) -> None:
    """Aplica DDL idempotente. Divide por GO (separador de batches de SSMS)."""
    sql = DDL_PATH.read_text(encoding="utf-8")
    batches = [b.strip() for b in sql.split("\nGO\n") if b.strip()]
    cur = cn.cursor()
    for b in batches:
        cur.execute(b)
    cn.commit()
    print(f"[ddl] OK ({len(batches)} batches)")


def find_xlsx(arg: str | None) -> Path:
    if arg:
        p = Path(arg) if Path(arg).is_absolute() else Path(arg)
        if p.exists():
            return p
        for d in SEARCH_DIRS:
            cand = d / arg
            if cand.exists():
                return cand
        raise FileNotFoundError(arg)
    for d in SEARCH_DIRS:
        cands = sorted(d.glob("datos_eta_*.xlsx"))
        if cands:
            return cands[-1]
    raise FileNotFoundError("No se encontró datos_eta_*.xlsx en " + ", ".join(map(str, SEARCH_DIRS)))


def load_simpli(cn: pyodbc.Connection, df: pd.DataFrame) -> int:
    df = df[SIMPLI_COLS].copy()
    before = len(df)
    df = df.drop_duplicates(subset=["id"], keep="first")
    if len(df) != before:
        print(f"[simpli] dedupe: {before} -> {len(df)} filas (id duplicados eliminados)")
    df["planned_date"] = pd.to_datetime(df["planned_date"]).dt.date
    df["checkout_cl"] = pd.to_datetime(df["checkout_cl"])
    df["current_eta_cl"] = pd.to_datetime(df["current_eta_cl"])
    for c in ("checkout_comment", "checkout_observation"):
        df[c] = df[c].astype(object).where(df[c].notna(), None)
    # BIT columns: cast a int 0/1
    for c in (
        "fechas_futuras_bq", "finicio_currenteta_bq",
        "current_eta_cl_fechainicioruta_dates",
        "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
        "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
        "ruta_anomala",
    ):
        df[c] = df[c].astype(int)

    dates = df["planned_date"].unique().tolist()
    cur = cn.cursor()
    cur.fast_executemany = True
    placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
    cols_sql = ", ".join(f"[{c}]" for c in SIMPLI_COLS)
    for d in dates:
        cur.execute("DELETE FROM fpoc.simpli_visits WHERE planned_date = ?", d)
    rows = [tuple(None if pd.isna(v) else v for v in row) for row in df.itertuples(index=False, name=None)]
    cur.executemany(f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})", rows)
    cn.commit()
    print(f"[simpli] insert {len(rows)} (fechas reemplazadas: {dates})")
    return len(rows)


def load_geo(cn: pyodbc.Connection, df: pd.DataFrame) -> int:
    df = df[GEO_COLS].copy()
    before = len(df)
    df = df.drop_duplicates(subset=["Suborden"], keep="first")
    if len(df) != before:
        print(f"[geo] dedupe: {before} -> {len(df)} filas (Suborden duplicados eliminados)")
    df["fechapactada"] = pd.to_datetime(df["fechapactada"]).dt.date
    for c in ("lpn", "parentorder"):
        df[c] = df[c].astype("Int64")
    for c in ("motivonoentrega", "comentarionoentrega"):
        df[c] = df[c].astype(object).where(df[c].notna(), None)

    cur = cn.cursor()
    cur.fast_executemany = True
    placeholders = ", ".join(["?"] * len(GEO_COLS))
    cols_sql = ", ".join(f"[{c}]" for c in GEO_COLS)
    # TRUNCATE total: Geo no tiene PK por fecha; se reemplaza todo el set.
    # Como la PK es Suborden, preferimos DELETE por idruta presentes.
    rutas = df["idruta"].unique().tolist()
    batch = 1000
    for i in range(0, len(rutas), batch):
        chunk = rutas[i:i + batch]
        marks = ",".join(["?"] * len(chunk))
        cur.execute(f"DELETE FROM fpoc.geo_suborders WHERE idruta IN ({marks})", *chunk)
    rows = [tuple(None if pd.isna(v) else v for v in row) for row in df.itertuples(index=False, name=None)]
    cur.executemany(f"INSERT INTO fpoc.geo_suborders ({cols_sql}) VALUES ({placeholders})", rows)
    cn.commit()
    print(f"[geo] insert {len(rows)} (idrutas reemplazadas: {len(rutas)})")
    return len(rows)


def verify(cn: pyodbc.Connection) -> None:
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc.simpli_visits")
    n_s = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM fpoc.geo_suborders")
    n_g = cur.fetchone()[0]
    cur.execute("SELECT TOP 1 status, COUNT(*) OVER() FROM fpoc.simpli_visits")
    r = cur.fetchone()
    cur.execute("SELECT SUM(CAST(ruta_anomala AS INT)) FROM fpoc.simpli_visits")
    n_anom = cur.fetchone()[0]
    print(f"[verify] simpli_visits={n_s} geo_suborders={n_g} ruta_anomala=1: {n_anom}")


def main(argv: list[str]) -> int:
    xlsx = find_xlsx(argv[1] if len(argv) > 1 else None)
    print(f"[file] {xlsx}")
    df_simpli = pd.read_excel(xlsx, sheet_name="Simpli")
    df_geo = pd.read_excel(xlsx, sheet_name="Geo")
    print(f"[file] Simpli={len(df_simpli)} Geo={len(df_geo)}")

    with get_conn() as cn:
        apply_ddl(cn)
        load_simpli(cn, df_simpli)
        load_geo(cn, df_geo)
        verify(cn)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

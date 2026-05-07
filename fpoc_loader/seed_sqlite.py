"""Seed local de la base SQLite para el POC ValueData.

Crea (o actualiza) `valuedata_backend/valuedata.db` con:
  - Schema completo (sqlite_schema.sql)
  - Datos del Excel datos_eta_*.xlsx -> fpoc_simpli_visits + fpoc_geo_suborders
  - Empresas distintas detectadas en simpli_visits -> fpoc_empresas_transporte
  - 1 admin Falabella + 1 ops Falabella + 1 transport_manager por empresa

Usar:
    cd valuedata_backend
    python fpoc_loader/seed_sqlite.py                                # busca el último datos_eta_*.xlsx
    python fpoc_loader/seed_sqlite.py datos_eta_2026_04_19.xlsx       # archivo explícito

Idempotente: si la DB ya existe, reemplaza visits/geo de la(s) fecha(s) presentes
en el Excel y hace upsert de empresas/users.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from passlib.hash import bcrypt


HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SCHEMA_PATH = HERE / "sqlite_schema.sql"
DEFAULT_DB = BACKEND_ROOT / "valuedata.db"
SEARCH_DIRS = [
    BACKEND_ROOT.parent / "client" / "data",
    BACKEND_ROOT,
    BACKEND_ROOT.parent,
    Path.cwd(),
]


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


ADMIN = {
    "email": "admin@falabella.cl",
    "password": "admin123",
    "display_name": "Admin Falabella",
    "role": "falabella_admin",
    "empresa_id": None,
}
OPS = {
    "email": "ops@falabella.cl",
    "password": "ops123",
    "display_name": "Operaciones Falabella",
    "role": "falabella_ops",
    "empresa_id": None,
}
DEFAULT_TRANSPORT_PASSWORD = "demo123"


def get_db_path() -> Path:
    return Path(os.environ.get("SQLITE_PATH", str(DEFAULT_DB)))


def get_conn(db_path: Path) -> sqlite3.Connection:
    cn = sqlite3.connect(db_path)
    cn.execute("PRAGMA foreign_keys = ON")
    return cn


def apply_schema(cn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    cn.executescript(sql)
    cn.commit()
    print("[schema] applied OK")


def find_xlsx(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
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


def _to_value(v):
    """Convierte NaN/NaT a None y Timestamps a string ISO compatible con sqlite3."""
    if pd.isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        # SQLite acepta 'YYYY-MM-DD HH:MM:SS[.ffffff]'
        return v.to_pydatetime()
    if hasattr(v, "isoformat"):
        return v
    return v


def load_simpli(cn: sqlite3.Connection, df: pd.DataFrame) -> int:
    df = df[SIMPLI_COLS].copy()
    before = len(df)
    df = df.drop_duplicates(subset=["id"], keep="first")
    if len(df) != before:
        print(f"[simpli] dedupe: {before} -> {len(df)}")

    df["planned_date"] = pd.to_datetime(df["planned_date"]).dt.date
    df["checkout_cl"] = pd.to_datetime(df["checkout_cl"])
    df["current_eta_cl"] = pd.to_datetime(df["current_eta_cl"])
    # SQLite no tiene type TIME; lo guardamos como string HH:MM:SS
    df["Fechainicioruta_hora_cl"] = df["Fechainicioruta_hora_cl"].apply(
        lambda v: v.strftime("%H:%M:%S") if hasattr(v, "strftime") else str(v) if pd.notna(v) else None
    )
    for c in (
        "fechas_futuras_bq", "finicio_currenteta_bq",
        "current_eta_cl_fechainicioruta_dates",
        "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
        "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
        "ruta_anomala",
    ):
        df[c] = df[c].astype(int)

    cur = cn.cursor()
    dates = sorted(df["planned_date"].unique().tolist())
    for d in dates:
        cur.execute("DELETE FROM fpoc_simpli_visits WHERE planned_date = ?", (d,))

    placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
    cols_sql = ", ".join(f'"{c}"' for c in SIMPLI_COLS)
    rows = [tuple(_to_value(v) for v in row) for row in df.itertuples(index=False, name=None)]
    cur.executemany(
        f"INSERT INTO fpoc_simpli_visits ({cols_sql}) VALUES ({placeholders})",
        rows,
    )
    cn.commit()
    print(f"[simpli] insert {len(rows)} (fechas reemplazadas: {dates})")
    return len(rows)


def load_geo(cn: sqlite3.Connection, df: pd.DataFrame) -> int:
    df = df[GEO_COLS].copy()
    before = len(df)
    df = df.drop_duplicates(subset=["Suborden"], keep="first")
    if len(df) != before:
        print(f"[geo] dedupe: {before} -> {len(df)}")

    df["fechapactada"] = pd.to_datetime(df["fechapactada"]).dt.date
    for c in ("lpn", "parentorder"):
        df[c] = df[c].astype("Int64")

    cur = cn.cursor()
    rutas = df["idruta"].unique().tolist()
    BATCH = 500
    for i in range(0, len(rutas), BATCH):
        chunk = rutas[i:i + BATCH]
        marks = ",".join(["?"] * len(chunk))
        cur.execute(f"DELETE FROM fpoc_geo_suborders WHERE idruta IN ({marks})", tuple(chunk))

    placeholders = ", ".join(["?"] * len(GEO_COLS))
    cols_sql = ", ".join(f'"{c}"' for c in GEO_COLS)
    rows = [tuple(_to_value(v) for v in row) for row in df.itertuples(index=False, name=None)]
    cur.executemany(
        f"INSERT INTO fpoc_geo_suborders ({cols_sql}) VALUES ({placeholders})",
        rows,
    )
    cn.commit()
    print(f"[geo] insert {len(rows)} (idrutas reemplazadas: {len(rutas)})")
    return len(rows)


def seed_empresas(cn: sqlite3.Connection) -> list[int]:
    cur = cn.cursor()
    cur.execute("SELECT DISTINCT Empresa_falsa FROM fpoc_simpli_visits ORDER BY Empresa_falsa")
    ids = [int(r[0]) for r in cur.fetchall()]
    for eid in ids:
        nombre = f"Transporte {eid:02d}"
        cur.execute(
            """
            INSERT INTO fpoc_empresas_transporte (empresa_id, nombre)
            VALUES (?, ?)
            ON CONFLICT(empresa_id) DO UPDATE SET nombre = excluded.nombre
            """,
            (eid, nombre),
        )
    cn.commit()
    print(f"[empresas] seed {len(ids)} empresas (IDs: {ids[:5]}{'...' if len(ids) > 5 else ''})")
    return ids


def upsert_user(cn: sqlite3.Connection, *, email: str, password: str,
                 display_name: str, role: str, empresa_id: int | None) -> None:
    pwd_hash = bcrypt.hash(password)
    cur = cn.cursor()
    cur.execute(
        """
        INSERT INTO fpoc_users (email, password_hash, display_name, role, empresa_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            password_hash = excluded.password_hash,
            display_name  = excluded.display_name,
            role          = excluded.role,
            empresa_id    = excluded.empresa_id,
            activo        = 1
        """,
        (email, pwd_hash, display_name, role, empresa_id),
    )
    cn.commit()


def seed_maestros(cn: sqlite3.Connection) -> None:
    """Seedea drivers/vehicles/clients usando los generadores deterministicos de
    pipeline.py + masters.py. Idempotente: solo inserta si la tabla esta vacia."""
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        from pipeline import gen_customer_pool
        from masters import gen_drivers, gen_vehicles_extended
    finally:
        sys.path.pop(0)

    cur = cn.cursor()

    # Drivers
    cur.execute("SELECT COUNT(*) FROM fpoc_drivers")
    if cur.fetchone()[0] == 0:
        drivers = gen_drivers()
        rows = [
            (
                d["driver_id"], d["name"], d["phone"], d["license"],
                int(d["vehicle_id"]), d["vehicle_name"],
                float(d["rating"]), int(d["deliveries_30d"]),
                float(d["fail_rate_30d"]), d["joined_at"],
                1 if d["active"] else 0,
                1 if d.get("is_problem_hidden", False) else 0,
            )
            for d in drivers
        ]
        cur.executemany(
            """INSERT INTO fpoc_drivers
                (driver_id, name, phone, license, vehicle_id, vehicle_name,
                 rating, deliveries_30d, fail_rate_30d, joined_at, active, is_problem_hidden)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        print(f"[drivers] seed {len(rows)}")
    else:
        drivers = None
        print("[drivers] ya seedeado, skip")

    # Vehicles
    cur.execute("SELECT COUNT(*) FROM fpoc_vehicles")
    if cur.fetchone()[0] == 0:
        if drivers is None:
            drivers = gen_drivers()
        vehicles = gen_vehicles_extended(drivers)
        rows = [
            (
                int(v["vehicle_id"]), v["name"], v["type"], v["plate"],
                int(v["capacity_m3"]), v["driver_id"], v["driver_name"],
                float(v["depot_lat"]), float(v["depot_lon"]),
                int(v["year"]),
                1 if v["active"] else 0,
                1 if v.get("is_problem_hidden", False) else 0,
            )
            for v in vehicles
        ]
        cur.executemany(
            """INSERT INTO fpoc_vehicles
                (vehicle_id, name, type, plate, capacity_m3, driver_id, driver_name,
                 depot_lat, depot_lon, year, active, is_problem_hidden)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        print(f"[vehicles] seed {len(rows)}")
    else:
        print("[vehicles] ya seedeado, skip")

    # Clients
    cur.execute("SELECT COUNT(*) FROM fpoc_clients")
    if cur.fetchone()[0] == 0:
        customers = gen_customer_pool()
        rows = [
            (
                c["customer_id"], c["title"], c["address"],
                float(c["latitude"]), float(c["longitude"]),
                1 if c.get("_is_recurrent", False) else 0,
                1 if c.get("_in_problem_comuna", False) else 0,
                None,
            )
            for c in customers
        ]
        cur.executemany(
            """INSERT INTO fpoc_clients
                (customer_id, title, address, latitude, longitude,
                 is_recurrent, in_problem_comuna, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        print(f"[clients] seed {len(rows)}")
    else:
        print("[clients] ya seedeado, skip")

    cn.commit()


def verify(cn: sqlite3.Connection) -> None:
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc_simpli_visits")
    n_s = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM fpoc_geo_suborders")
    n_g = cur.fetchone()[0]
    cur.execute("SELECT SUM(CAST(ruta_anomala AS INTEGER)) FROM fpoc_simpli_visits")
    n_anom = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM fpoc_empresas_transporte")
    n_e = cur.fetchone()[0]
    cur.execute("SELECT role, COUNT(*) FROM fpoc_users GROUP BY role")
    roles = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) FROM fpoc_drivers")
    n_drv = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM fpoc_vehicles")
    n_veh = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM fpoc_clients")
    n_cli = cur.fetchone()[0]
    print(f"[verify] simpli={n_s} geo={n_g} anomalas={n_anom} empresas={n_e} "
          f"users={roles} drivers={n_drv} vehicles={n_veh} clients={n_cli}")


def main(argv: list[str]) -> int:
    db_path = get_db_path()
    print(f"[db] {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    xlsx = find_xlsx(argv[1] if len(argv) > 1 else None)
    print(f"[file] {xlsx}")
    df_simpli = pd.read_excel(xlsx, sheet_name="Simpli")
    df_geo = pd.read_excel(xlsx, sheet_name="Geo")
    print(f"[file] Simpli={len(df_simpli)} Geo={len(df_geo)}")

    with get_conn(db_path) as cn:
        apply_schema(cn)
        load_simpli(cn, df_simpli)
        load_geo(cn, df_geo)
        empresa_ids = seed_empresas(cn)
        upsert_user(cn, **ADMIN)
        upsert_user(cn, **OPS)
        for eid in empresa_ids:
            upsert_user(
                cn,
                email=f"transporte{eid:02d}@demo.cl",
                password=DEFAULT_TRANSPORT_PASSWORD,
                display_name=f"Manager Transporte {eid:02d}",
                role="transport_manager",
                empresa_id=eid,
            )
        seed_maestros(cn)
        verify(cn)

    print("\n[users] credenciales para login:")
    print(f"  admin: {ADMIN['email']} / {ADMIN['password']}")
    print(f"  ops:   {OPS['email']} / {OPS['password']}")
    print(f"  transporte<NN>@demo.cl / {DEFAULT_TRANSPORT_PASSWORD} (ej: transporte01@demo.cl)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

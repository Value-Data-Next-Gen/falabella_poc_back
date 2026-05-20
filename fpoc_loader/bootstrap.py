"""Bootstrap automático de SQLite al startup del backend.

Si la DB SQLite no existe (o existe pero está vacía / sin tablas), esta función:

  1. Aplica el DDL completo (sqlite_schema.sql)
  2. Seedea admin + ops + transport_managers en fpoc_users
  3. Si encuentra `datos_eta_*.xlsx`: corre el seed completo del Excel
     (fpoc_simpli_visits + fpoc_geo_suborders + clients/drivers/vehicles)
  4. Si NO encuentra el Excel: genera dataset sintético mínimo (~7 días × 50)
     usando los generadores deterministicos de pipeline.py
  5. Aplica las 5 migraciones nuevas (drivers_whatsapp, vip_deadline, etc.)

Idempotente: si la DB ya tiene datos, no hace nada (salvo aplicar
migraciones faltantes, que ya son idempotentes en sí mismas).

Sólo aplica con `DB_BACKEND=sqlite`. En SQL Server (Azure) es no-op.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from loguru import logger
from passlib.hash import bcrypt


HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SCHEMA_PATH = HERE / "sqlite_schema.sql"


# ---------- Default users (mismo set que seed_sqlite.py) ----------
ADMIN_USER = {
    "email": "admin@falabella.cl",
    "password": "admin123",
    "display_name": "Admin Falabella",
    "role": "falabella_admin",
    "empresa_id": None,
}
OPS_USER = {
    "email": "ops@falabella.cl",
    "password": "ops123",
    "display_name": "Operaciones Falabella",
    "role": "falabella_ops",
    "empresa_id": None,
}
DEFAULT_TRANSPORT_PASSWORD = "demo123"
DEFAULT_EMPRESA_IDS_SYNTHETIC = [1, 2, 3]


# =============================================================================
# Helpers internos
# =============================================================================
def _sqlite_path() -> Path:
    return Path(os.environ.get(
        "SQLITE_PATH",
        str(BACKEND_ROOT / "valuedata.db"),
    ))


def _is_sqlite_backend() -> bool:
    return os.environ.get("DB_BACKEND", "sqlite").lower() == "sqlite"


def _open_raw_sqlite(db_path: Path) -> sqlite3.Connection:
    cn = sqlite3.connect(str(db_path), timeout=30.0)
    cn.execute("PRAGMA foreign_keys = ON")
    return cn


def _table_count(cn: sqlite3.Connection) -> int:
    cur = cn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name LIKE 'fpoc_%'"
    )
    return int(cur.fetchone()[0])


def _row_count(cn: sqlite3.Connection, table: str) -> int:
    try:
        cur = cn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    except sqlite3.Error:
        return 0


# =============================================================================
# Pasos de bootstrap
# =============================================================================
def _apply_schema(cn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    cn.executescript(sql)
    cn.commit()


def _seed_users_minimal(cn: sqlite3.Connection, empresa_ids: list[int]) -> None:
    """Inserta admin + ops + 1 transport_manager por empresa_id."""
    cur = cn.cursor()

    # Admin + Ops
    for u in (ADMIN_USER, OPS_USER):
        pwd_hash = bcrypt.hash(u["password"])
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
            (u["email"], pwd_hash, u["display_name"], u["role"], u["empresa_id"]),
        )

    # Transport managers
    for eid in empresa_ids:
        email = f"transporte{eid:02d}@demo.cl"
        pwd_hash = bcrypt.hash(DEFAULT_TRANSPORT_PASSWORD)
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
            (email, pwd_hash, f"Manager Transporte {eid:02d}", "transport_manager", eid),
        )
    cn.commit()


def _seed_empresas_synthetic(cn: sqlite3.Connection, ids: list[int]) -> None:
    cur = cn.cursor()
    for eid in ids:
        cur.execute(
            """
            INSERT INTO fpoc_empresas_transporte (empresa_id, nombre)
            VALUES (?, ?)
            ON CONFLICT(empresa_id) DO UPDATE SET nombre = excluded.nombre
            """,
            (eid, f"Transporte {eid:02d}"),
        )
    cn.commit()


def _find_excel() -> Path | None:
    """Busca datos_eta_*.xlsx en client/data/, backend/, project root y cwd."""
    search_dirs = [
        BACKEND_ROOT.parent / "client" / "data",
        BACKEND_ROOT,
        BACKEND_ROOT.parent,
        Path.cwd(),
    ]
    for d in search_dirs:
        cands = sorted(d.glob("datos_eta_*.xlsx"))
        if cands:
            return cands[-1]
    return None


def _load_excel_via_seed_sqlite(db_path: Path, xlsx: Path) -> None:
    """Llama a seed_sqlite.main() pasándole el archivo. Maneja sus propios
    print() y commits."""
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    # Apuntamos SQLITE_PATH explícitamente para que seed_sqlite use la misma DB
    prev_path = os.environ.get("SQLITE_PATH")
    os.environ["SQLITE_PATH"] = str(db_path)
    try:
        from fpoc_loader import seed_sqlite as _seed
        _seed.main(["seed_sqlite", str(xlsx)])
    finally:
        if prev_path is None:
            os.environ.pop("SQLITE_PATH", None)
        else:
            os.environ["SQLITE_PATH"] = prev_path


def _generate_synthetic_data(cn: sqlite3.Connection) -> int:
    """Genera ~7 días × 50 visitas usando los generadores de pipeline.py.
    Devuelve el número de filas insertadas en fpoc_simpli_visits.

    Se usa SOLO cuando no se encuentra el Excel original. El objetivo es que la
    app no se rompa: deja datos mínimos para login + plan + maestros.
    """
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    import random
    from datetime import date, datetime, time, timedelta

    try:
        from ml.pipeline import gen_customer_pool, gen_day_visits  # type: ignore
        from ml.masters import gen_drivers, gen_vehicles_extended  # type: ignore
    except Exception as e:
        logger.warning(f"[bootstrap] no pude importar generadores sintéticos: {e}")
        return 0

    rng = random.Random(42)

    # Empresas mínimas (vienen tagged en gen_day_visits via vehicle_id)
    _seed_empresas_synthetic(cn, DEFAULT_EMPRESA_IDS_SYNTHETIC)

    # Pool de clientes
    customers = gen_customer_pool()
    today = date.today()

    # Drivers + vehicles
    cur = cn.cursor()
    if _row_count(cn, "fpoc_drivers") == 0:
        drivers = gen_drivers()
        cur.executemany(
            """INSERT OR IGNORE INTO fpoc_drivers
                (driver_id, name, phone, license, vehicle_id, vehicle_name,
                 rating, deliveries_30d, fail_rate_30d, joined_at, active, is_problem_hidden)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    d["driver_id"], d["name"], d["phone"], d["license"],
                    int(d["vehicle_id"]), d["vehicle_name"],
                    float(d["rating"]), int(d["deliveries_30d"]),
                    float(d["fail_rate_30d"]), d["joined_at"],
                    1 if d["active"] else 0,
                    1 if d.get("is_problem_hidden", False) else 0,
                )
                for d in drivers
            ],
        )
    else:
        drivers = gen_drivers()  # for vehicles below

    if _row_count(cn, "fpoc_vehicles") == 0:
        vehicles = gen_vehicles_extended(drivers)
        cur.executemany(
            """INSERT OR IGNORE INTO fpoc_vehicles
                (vehicle_id, name, type, plate, capacity_m3, driver_id, driver_name,
                 depot_lat, depot_lon, year, active, is_problem_hidden)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    int(v["vehicle_id"]), v["name"], v["type"], v["plate"],
                    int(v["capacity_m3"]), v["driver_id"], v["driver_name"],
                    float(v["depot_lat"]), float(v["depot_lon"]),
                    int(v["year"]),
                    1 if v["active"] else 0,
                    1 if v.get("is_problem_hidden", False) else 0,
                )
                for v in vehicles
            ],
        )

    if _row_count(cn, "fpoc_clients") == 0:
        cur.executemany(
            """INSERT OR IGNORE INTO fpoc_clients
                (customer_id, title, address, latitude, longitude,
                 is_recurrent, in_problem_comuna, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    c["customer_id"], c["title"], c["address"],
                    float(c["latitude"]), float(c["longitude"]),
                    1 if c.get("_is_recurrent", False) else 0,
                    1 if c.get("_in_problem_comuna", False) else 0,
                    None,
                )
                for c in customers
            ],
        )

    cn.commit()

    # Generar 7 días de visitas sintéticas tableadas en fpoc_simpli_visits.
    # gen_day_visits devuelve un DataFrame con columnas pipeline-friendly,
    # NO con el shape exacto del Excel real. Para no romper invariantes
    # rellenamos los campos requeridos del schema con valores plausibles.
    SIMPLI_COLS = [
        "planned_date", "id", "title", "order", "address",
        "checkout_cl", "current_eta_cl", "status",
        "checkout_comment", "checkout_observation", "reference", "country",
        "sla_hour_checkout_eta", "bin_start", "bin_end", "bin_label", "bin_index",
        "ct", "patente_falsa", "empresa_falsa", "driver_name",
        "fecha_inicio_ruta", "fecha_inicio_ruta_hora_cl",
        "fechas_futuras_bq", "finicio_currenteta_bq",
        "current_eta_cl_fechainicioruta", "current_eta_cl_fechainicioruta_dates",
        "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
        "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
        "am_pm", "ruta_anomala",
    ]
    placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
    cols_sql = ", ".join(f'"{c}"' for c in SIMPLI_COLS)

    inserted = 0
    next_id = 1_000_000_000  # offset alto para no colisionar con datos reales
    for d_offset in range(7):
        target_date = today - timedelta(days=d_offset)
        df = gen_day_visits(d_offset, target_date, customers).head(50)
        rows: list[tuple[Any, ...]] = []
        for i, (_, vrow) in enumerate(df.iterrows()):
            empresa_id = DEFAULT_EMPRESA_IDS_SYNTHETIC[
                i % len(DEFAULT_EMPRESA_IDS_SYNTHETIC)
            ]
            patente = 1000 + (i % 20)
            drivername = f"Driver Sintetico {i % 20:02d}"
            checkout_dt = datetime.combine(target_date, time(rng.randint(9, 17), rng.randint(0, 59)))
            eta_dt = checkout_dt + timedelta(hours=1)
            sla = round(rng.uniform(0.5, 6.0), 4)
            bs = (sla // 0.5) * 0.5
            be = bs + 0.5
            row = (
                target_date,                           # planned_date
                next_id,                               # id
                str(vrow.get("title", f"Cliente {i}")),
                int(vrow.get("order", i + 1)),
                str(vrow.get("address", "Sin dirección")),
                checkout_dt,
                eta_dt,
                str(vrow.get("status", "completed")),
                None,
                None,
                int(rng.randint(100000, 999999)),     # reference
                "Chile",
                sla, bs, be, f"[{bs}, {be}]", int(40 + bs * 2),
                "CT-Synth",
                patente,
                empresa_id,
                drivername,
                target_date.isoformat(),
                "09:00:00",
                0, 0, 0, 0, 0, 0, 0, 0,
                "AM" if checkout_dt.hour < 12 else "PM",
                0,                                     # ruta_anomala
            )
            rows.append(row)
            next_id += 1
        cur.executemany(
            f"INSERT OR IGNORE INTO fpoc_simpli_visits ({cols_sql}) VALUES ({placeholders})",
            rows,
        )
        inserted += len(rows)
    cn.commit()
    return inserted


def _run_migrations() -> list[str]:
    """Aplica las 5 migraciones nuevas + seed_regiones_estacionalidad.

    Cada migración es idempotente. Devuelve lista de nombres ejecutados (para
    el log).
    """
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    migrations: list[tuple[str, Any]] = []
    try:
        from fpoc_loader import migrate_empresa_contactos as m1
        migrations.append(("migrate_empresa_contactos", m1.main))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bootstrap] no pude importar migrate_empresa_contactos: {e}")
    try:
        from fpoc_loader import migrate_vip_deadline as m2
        migrations.append(("migrate_vip_deadline", m2.main))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bootstrap] no pude importar migrate_vip_deadline: {e}")
    try:
        from fpoc_loader import migrate_motivo_corrections as m3
        migrations.append(("migrate_motivo_corrections", m3.main))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bootstrap] no pude importar migrate_motivo_corrections: {e}")
    try:
        from fpoc_loader import migrate_drivers_whatsapp as m4
        migrations.append(("migrate_drivers_whatsapp", m4.main))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bootstrap] no pude importar migrate_drivers_whatsapp: {e}")
    try:
        from fpoc_loader import seed_regiones_estacionalidad as m5
        migrations.append(("seed_regiones_estacionalidad", m5.main))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bootstrap] no pude importar seed_regiones_estacionalidad: {e}")

    ran: list[str] = []
    for name, fn in migrations:
        try:
            rc = fn()
            ran.append(f"{name}(rc={rc})")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bootstrap] {name} falló: {e}")
    return ran


# =============================================================================
# Punto de entrada
# =============================================================================
def bootstrap_if_needed() -> None:
    """Inspecciona la DB y, si está vacía o no existe, la inicializa.

    Pasos:
      1. Aplicar schema (idempotente)
      2. Si fpoc_simpli_visits está vacía: cargar Excel o generar sintético
      3. Seedear users (admin + ops + transport_managers)
      4. Aplicar migraciones nuevas

    No-op para DB_BACKEND != sqlite.
    """
    if not _is_sqlite_backend():
        logger.info("[bootstrap] DB_BACKEND != sqlite, skip bootstrap")
        return

    db_path = _sqlite_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_existed = db_path.exists()

    pasos: list[str] = []

    # 1) Schema
    with _open_raw_sqlite(db_path) as cn:
        n_tables_before = _table_count(cn)
        if n_tables_before == 0:
            logger.info(f"[bootstrap] DB sin tablas (existed={db_existed}), aplicando schema...")
            _apply_schema(cn)
            pasos.append("schema")
        else:
            # Re-ejecutamos el schema porque es idempotente (CREATE IF NOT EXISTS)
            # — esto cubre el caso "DB con algunas tablas pero faltan tablas nuevas".
            _apply_schema(cn)

        n_visits = _row_count(cn, "fpoc_simpli_visits")
        n_users = _row_count(cn, "fpoc_users")

    # 2) Datos: solo si no hay visitas
    if n_visits == 0:
        xlsx = _find_excel()
        if xlsx is not None:
            logger.info(f"[bootstrap] cargando Excel: {xlsx.name}")
            try:
                _load_excel_via_seed_sqlite(db_path, xlsx)
                pasos.append(f"seed_sqlite({xlsx.name})")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[bootstrap] seed_sqlite falló: {e}, fallback a sintético")
                with _open_raw_sqlite(db_path) as cn:
                    n = _generate_synthetic_data(cn)
                pasos.append(f"synthetic_data({n} visitas)")
        else:
            logger.info("[bootstrap] no se encontró datos_eta_*.xlsx, generando sintético")
            with _open_raw_sqlite(db_path) as cn:
                n = _generate_synthetic_data(cn)
            pasos.append(f"synthetic_data({n} visitas)")
    else:
        logger.info(f"[bootstrap] {n_visits} visitas ya en DB, skip seed de datos")

    # 3) Users (siempre garantizamos admin + transport_managers para empresas
    #    presentes en fpoc_empresas_transporte).
    with _open_raw_sqlite(db_path) as cn:
        cur = cn.execute("SELECT empresa_id FROM fpoc_empresas_transporte ORDER BY empresa_id")
        empresa_ids = [int(r[0]) for r in cur.fetchall()]
        if not empresa_ids:
            empresa_ids = list(DEFAULT_EMPRESA_IDS_SYNTHETIC)
            _seed_empresas_synthetic(cn, empresa_ids)

        n_users_before = _row_count(cn, "fpoc_users")
        if n_users_before == 0:
            _seed_users_minimal(cn, empresa_ids)
            pasos.append(f"seed_users({len(empresa_ids) + 2} users)")
        else:
            # Garantizamos que admin existe (idempotente)
            _seed_users_minimal(cn, empresa_ids)

    # 4) Migraciones (siempre — todas son idempotentes)
    ran = _run_migrations()
    if ran:
        pasos.append(f"migrations({', '.join(ran)})")

    if pasos:
        logger.info(f"[bootstrap] OK: {' | '.join(pasos)}")
    else:
        logger.info("[bootstrap] DB ya inicializada, no se aplicaron pasos nuevos")


__all__ = ["bootstrap_if_needed"]

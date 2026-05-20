"""Migración: regiones + estacionalidad + ruta_id sobre fpoc_simpli_visits.

Sprint 6 — bug tracker del cliente:
  - Solo se ven datos de RM. Insertar visitas en regiones (Valparaíso, Biobío,
    Araucanía, Coquimbo, Antofagasta, Maule).
  - No hay días pico. Generar Black Friday + Cyber Week.
  - Ruta como entidad: agregar columna `ruta_id` (R-YYYYMMDD-NNN) y backfillar.
  - Agregar columnas `region` + `comuna` (parseadas desde `address`).

Idempotente: corre múltiples veces sin duplicar.

Uso:
    python valuedata_backend/fpoc_loader/seed_regiones_estacionalidad.py

Salida esperada:
    [seed-regiones] +X visitas regiones, +Y visitas Black Friday/Cyber Week,
    +Z columnas geo. Total dataset: NN.
"""
from __future__ import annotations

import calendar
import random
import sys
from datetime import date, datetime, timedelta
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


# =============================================================================
# Catálogo de comunas RM (parseo address → comuna)
# =============================================================================
# Orden importa: las más específicas primero para evitar falsos positivos
RM_COMUNAS = [
    "Quilicura", "Las Condes", "Providencia", "Puente Alto", "San Bernardo",
    "La Florida", "Maipú", "Maipu", "Ñuñoa", "Nunoa", "Independencia",
    "Recoleta", "Estación Central", "Estacion Central", "Cerrillos",
    "Pudahuel", "Renca", "Lo Espejo", "El Bosque", "La Pintana",
    "La Cisterna", "San Miguel", "San Joaquín", "San Joaquin", "Macul",
    "Peñalolén", "Penalolen", "La Reina", "Vitacura", "Lo Barnechea",
    "Huechuraba", "Conchalí", "Conchali", "Quinta Normal", "Lo Prado",
    "Cerro Navia", "Pedro Aguirre Cerda", "Santiago",
]

# =============================================================================
# Comunas regiones (para inserts nuevos)
# Cada tupla = (comuna, region, ct, address_template)
# =============================================================================
REGIONES_DATA = [
    # Valparaíso
    ("Viña del Mar", "Valparaíso", "CD CENTRO", ["Av. San Martín {n}", "1 Norte {n}", "Calle Valparaíso {n}", "5 Poniente {n}"]),
    ("Valparaíso", "Valparaíso", "CD CENTRO", ["Av. Brasil {n}", "Pedro Montt {n}", "Errázuriz {n}", "Independencia {n}"]),
    # Biobío
    ("Concepción", "Biobío", "CD SUR", ["Barros Arana {n}", "Av. Los Carrera {n}", "Caupolicán {n}", "O'Higgins {n}"]),
    ("Talcahuano", "Biobío", "CD SUR", ["Colón {n}", "Av. Gran Bretaña {n}", "Almirante Latorre {n}"]),
    # Araucanía
    ("Temuco", "Araucanía", "CD SUR", ["Manuel Bulnes {n}", "Caupolicán {n}", "Av. Alemania {n}", "Prieto Norte {n}"]),
    # Coquimbo
    ("Coquimbo", "Coquimbo", "CD NORTE", ["Aldunate {n}", "Av. Costanera {n}", "Bilbao {n}"]),
    ("La Serena", "Coquimbo", "CD NORTE", ["Av. Francisco de Aguirre {n}", "Cordovez {n}", "Balmaceda {n}"]),
    # Antofagasta
    ("Antofagasta", "Antofagasta", "CD NORTE", ["Av. Argentina {n}", "Prat {n}", "Av. Brasil {n}", "Latorre {n}"]),
    # Maule
    ("Talca", "Maule", "CD CENTRO", ["1 Sur {n}", "2 Norte {n}", "Av. San Miguel {n}", "11 Oriente {n}"]),
    ("Curicó", "Maule", "CD CENTRO", ["Carmen {n}", "Av. Manso de Velasco {n}", "Yungay {n}"]),
    # O'Higgins
    ("Rancagua", "O'Higgins", "CD CENTRO", ["Av. Cachapoal {n}", "Estado {n}", "Independencia {n}", "Av. Brasil {n}"]),
]

DRIVER_POOL_REGIONES = [
    "Cristóbal Alexis", "Juan González", "María Silva", "Pedro Rojas",
    "Ana Contreras", "Carlos Muñoz", "Sofía Díaz", "Andrés Figueroa",
    "Paula Castro", "Ricardo Tapia", "Felipe Soto", "Camila Vega",
    "Diego Ramírez", "Valentina Pérez", "Matías Herrera", "Javiera Núñez",
    "Sebastián Lagos", "Constanza Fuentes", "Tomás Bravo", "Daniela Cáceres",
]

CLIENTES_POOL = [
    "Carolina Martínez", "Roberto Aros", "Pilar Faundes", "José Salazar",
    "Pablo Navarro", "Scarlett Fernández", "Luis Hernández", "Marcela López",
    "Andrea Pinto", "Gabriel Vargas", "Natalia Riquelme", "Cristian Pizarro",
    "Mónica Sandoval", "Esteban Quiroz", "Verónica Toro", "Iván Espinoza",
    "Patricia Aguilera", "Rodrigo Maldonado", "Bárbara Cortés", "Eduardo Rivas",
]

EMPRESAS_VALIDAS = [22, 23, 25, 27, 33]


# =============================================================================
# Helpers
# =============================================================================
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


def _parse_comuna_rm(address: str) -> str:
    """Match comuna RM dentro del address. Devuelve la primera coincidencia."""
    if not address:
        return "Santiago"
    addr_lower = address.lower()
    for c in RM_COMUNAS:
        if c.lower() in addr_lower:
            return c.replace("Maipu", "Maipú").replace("Nunoa", "Ñuñoa") \
                    .replace("Estacion Central", "Estación Central") \
                    .replace("Penalolen", "Peñalolén") \
                    .replace("Conchali", "Conchalí") \
                    .replace("San Joaquin", "San Joaquín")
    return "Santiago"


def _last_friday_of_november(year: int) -> date:
    """Black Friday: último viernes de noviembre."""
    # Encontrar el último día de noviembre
    last_day = calendar.monthrange(year, 11)[1]
    d = date(year, 11, last_day)
    # Retroceder hasta viernes (weekday=4)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def _add_columns_idempotent(cn) -> int:
    """Agrega columnas region, comuna, ruta_id si no existen. Devuelve el número agregadas."""
    added = 0
    cur = cn.cursor()
    for col_name, col_def in [
        ("region", "TEXT"),
        ("comuna", "TEXT"),
        ("ruta_id", "TEXT"),
    ]:
        if _column_exists(cn, "fpoc_simpli_visits", col_name):
            print(f"[skip] fpoc_simpli_visits.{col_name} ya existe")
            continue
        cur.execute(f"ALTER TABLE fpoc_simpli_visits ADD COLUMN {col_name} {col_def}")
        print(f"[ok] fpoc_simpli_visits.{col_name} agregado")
        added += 1
    cn.commit()

    # Index para acelerar lookups por ruta_id
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS IX_simpli_visits_ruta_id ON fpoc_simpli_visits (ruta_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS IX_simpli_visits_region ON fpoc_simpli_visits (region)")
        cn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] no pude crear índice: {e}")

    return added


def _backfill_region_comuna(cn) -> int:
    """Para visitas con region IS NULL: parsea address y setea region+comuna."""
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc_simpli_visits WHERE region IS NULL OR region = ''")
    n_pending = int(cur.fetchone()[0])
    if n_pending == 0:
        print("[backfill-region] sin filas pendientes")
        return 0

    print(f"[backfill-region] procesando {n_pending} visitas...")
    cur.execute(
        "SELECT id, address FROM fpoc_simpli_visits WHERE region IS NULL OR region = ''"
    )
    rows = cur.fetchall()
    updates = []
    for r in rows:
        comuna = _parse_comuna_rm(r.address or "")
        # default region RM (estado actual del dataset)
        updates.append(("RM", comuna, int(r.id)))

    cur.executemany(
        "UPDATE fpoc_simpli_visits SET region = ?, comuna = ? WHERE id = ?",
        updates,
    )
    cn.commit()
    print(f"[backfill-region] +{len(updates)} visitas con region/comuna")
    return len(updates)


def _backfill_ruta_id(cn) -> int:
    """Asigna ruta_id a TODAS las visitas que no lo tengan. ruta_id formato:
    R-YYYYMMDD-NNN donde NNN es secuencial dentro del día (numerado por
    orden alfabético de driver+patente para determinismo)."""
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc_simpli_visits WHERE ruta_id IS NULL OR ruta_id = ''")
    n_pending = int(cur.fetchone()[0])
    if n_pending == 0:
        print("[backfill-ruta] sin filas pendientes")
        return 0

    print(f"[backfill-ruta] procesando {n_pending} visitas...")

    # 1. Obtener combos (date, driver_name, patente_falsa) de visitas sin ruta_id
    cur.execute(
        """
        SELECT DISTINCT planned_date, driver_name, patente_falsa
        FROM fpoc_simpli_visits
        WHERE ruta_id IS NULL OR ruta_id = ''
        ORDER BY planned_date, driver_name, patente_falsa
        """
    )
    combos = [(r[0], r[1], int(r[2])) for r in cur.fetchall()]

    # Agrupar por planned_date para asignar NNN secuencial
    by_date: dict[str, list[tuple]] = {}
    for d, drv, pat in combos:
        # planned_date puede venir como str ("2026-04-28") o como date — normalizamos
        dkey = str(d)[:10]
        by_date.setdefault(dkey, []).append((drv, pat))

    update_pairs = []  # (ruta_id, planned_date, driver_name, patente_falsa)
    for dkey, rutas in by_date.items():
        # date sin guiones para el id
        dcompact = dkey.replace("-", "")
        # rutas ya viene ordenado dentro del SELECT pero re-aseguramos
        for idx, (drv, pat) in enumerate(sorted(rutas), start=1):
            rid = f"R-{dcompact}-{idx:03d}"
            update_pairs.append((rid, dkey, drv, pat))

    # Update por batch — usamos planned_date como string para hacer match
    cur.executemany(
        """
        UPDATE fpoc_simpli_visits
        SET ruta_id = ?
        WHERE planned_date = ? AND driver_name = ? AND patente_falsa = ?
              AND (ruta_id IS NULL OR ruta_id = '')
        """,
        update_pairs,
    )
    cn.commit()

    # Reportar
    cur.execute("SELECT COUNT(*) FROM fpoc_simpli_visits WHERE ruta_id IS NOT NULL AND ruta_id != ''")
    n_total = int(cur.fetchone()[0])
    print(f"[backfill-ruta] +{len(update_pairs)} rutas asignadas. Total con ruta_id: {n_total}")
    return len(update_pairs)


def _next_visit_id(cn) -> int:
    cur = cn.cursor()
    cur.execute("SELECT MAX(id) FROM fpoc_simpli_visits")
    r = cur.fetchone()
    base = int(r[0]) if r and r[0] is not None else 0
    return base + 1


def _bin_label_from_sla(sla: float) -> tuple[str, float, float, int]:
    """Genera bin_label/start/end/index a partir de sla_hour_checkout_eta.
    Bins de 0.5h centrados — replica el patrón del dataset original."""
    # Ej: 1.85 → [1.5, 2.0]
    start = round(sla * 2) / 2
    if start > sla:
        start -= 0.5
    end = start + 0.5
    label = f"[{start:.1f}, {end:.1f}]"
    idx = int(round(start * 2))
    return label, start, end, idx


def _gen_visita_regiones(
    visit_id: int,
    planned_date: date,
    ruta_id: str,
    order: int,
    empresa_id: int,
    drv: str,
    patente: int,
    fecha_inicio: datetime,
    comuna: str,
    region: str,
    ct: str,
    address: str,
    is_failed: bool = False,
) -> tuple:
    """Construye una tupla de visita compatible con el INSERT."""
    # ETA: fecha_inicio + offset random (1h-10h del día)
    rng = random.Random(visit_id)
    eta_offset = rng.uniform(1.0, 10.0)
    eta = fecha_inicio + timedelta(hours=eta_offset)
    sla = round(rng.uniform(-7.0, 8.0), 6)
    # checkout_cl: cerca del eta (algunos antes, otros después)
    checkout_offset = rng.uniform(-3.0, 4.0)
    checkout = eta + timedelta(hours=checkout_offset)
    bin_label, bin_start, bin_end, bin_idx = _bin_label_from_sla(sla)
    cliente = rng.choice(CLIENTES_POOL).lower()
    reference = 14000000 + rng.randint(0, 999999)
    am_pm = "AM" if eta.hour < 12 else "PM"
    status = "failed" if is_failed else "completed"

    return (
        planned_date.isoformat(),                                  # planned_date
        visit_id,                                                  # id
        cliente,                                                   # title
        order,                                                     # order
        address,                                                   # address
        checkout.strftime("%Y-%m-%d %H:%M:%S"),                    # checkout_cl
        eta.strftime("%Y-%m-%d %H:%M:%S"),                         # current_eta_cl
        status,                                                    # status
        None,                                                      # checkout_comment
        None,                                                      # checkout_observation
        reference,                                                 # reference
        "Chile",                                                   # country
        sla,                                                       # sla_hour_checkout_eta
        bin_start,                                                 # bin_start
        bin_end,                                                   # bin_end
        bin_label,                                                 # bin_label
        bin_idx,                                                   # bin_index
        ct,                                                        # ct
        patente,                                                   # patente_falsa
        empresa_id,                                                # empresa_falsa
        drv,                                                       # driver_name
        fecha_inicio.strftime("%Y-%m-%d %H:%M:%S.000000 UTC"),     # fecha_inicio_ruta
        fecha_inicio.strftime("%H:%M:%S"),                         # fecha_inicio_ruta_hora_cl
        0, 0, 0, 0, 0, 0, 0, 0,                                    # flags BQ (todos 0)
        am_pm,                                                     # am_pm
        0,                                                         # ruta_anomala
        region,                                                    # region
        comuna,                                                    # comuna
        ruta_id,                                                   # ruta_id
    )


SIMPLI_INSERT_COLS = [
    "planned_date", "id", "title", '"order"', "address", "checkout_cl",
    "current_eta_cl", "status", "checkout_comment", "checkout_observation",
    "reference", "country", "sla_hour_checkout_eta", "bin_start", "bin_end",
    "bin_label", "bin_index", "ct", "patente_falsa", "empresa_falsa",
    "driver_name", "fecha_inicio_ruta", "fecha_inicio_ruta_hora_cl",
    "fechas_futuras_bq", "finicio_currenteta_bq",
    "current_eta_cl_fechainicioruta", "current_eta_cl_fechainicioruta_dates",
    "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
    "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
    "am_pm", "ruta_anomala", "region", "comuna", "ruta_id",
]


def _insert_visitas_regiones(cn) -> int:
    """Inserta visitas en regiones. Idempotente: chequea si ya hay visitas con
    region != 'RM'. Si ya las hay, skip."""
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc_simpli_visits WHERE region IS NOT NULL AND region != 'RM' AND region != ''")
    n_existing = int(cur.fetchone()[0])
    if n_existing > 100:
        print(f"[seed-regiones] ya hay {n_existing} visitas en regiones, skip")
        return 0

    # Obtener distribución de fechas existentes
    cur.execute(
        "SELECT planned_date, COUNT(*) FROM fpoc_simpli_visits WHERE region = 'RM' GROUP BY planned_date ORDER BY planned_date"
    )
    rows = cur.fetchall()
    if not rows:
        print("[seed-regiones] sin fechas base, skip")
        return 0

    dates_with_count = [(str(r[0])[:10], int(r[1])) for r in rows]
    next_id = _next_visit_id(cn)
    rng = random.Random(2026)

    rutas_per_day = 18  # ~18 rutas/día en regiones
    visitas_per_ruta = (3, 7)
    inserted = 0
    insert_rows = []

    for d_str, rm_count in dates_with_count:
        d = date.fromisoformat(d_str)
        # 40% del volumen RM como visitas en regiones (cap a 18 rutas)
        target_rutas = min(rutas_per_day, max(5, int(rm_count * 0.4 / 5)))

        for r_idx in range(target_rutas):
            comuna, region, ct, addr_templates = rng.choice(REGIONES_DATA)
            drv = rng.choice(DRIVER_POOL_REGIONES)
            patente = rng.randint(1, 99)
            empresa_id = rng.choice(EMPRESAS_VALIDAS)
            fecha_inicio = datetime.combine(d, datetime.min.time()) + \
                timedelta(hours=rng.randint(6, 10), minutes=rng.randint(0, 59))
            n_visitas = rng.randint(*visitas_per_ruta)

            # ruta_id se asigna después por _backfill_ruta_id (deja en NULL → backfill lo cubre)
            for ord_n in range(1, n_visitas + 1):
                addr_tpl = rng.choice(addr_templates)
                address = addr_tpl.format(n=rng.randint(10, 9999)) + f" {comuna}"
                # Distribución status: 75% completed, 5% failed (~20% pending no aplica
                # porque el dataset original solo tiene completed/failed). Para datos
                # históricos: 5% failed.
                is_failed = rng.random() < 0.05
                row = _gen_visita_regiones(
                    visit_id=next_id,
                    planned_date=d,
                    ruta_id="",  # _backfill_ruta_id lo seteará después
                    order=ord_n,
                    empresa_id=empresa_id,
                    drv=drv,
                    patente=patente,
                    fecha_inicio=fecha_inicio,
                    comuna=comuna,
                    region=region,
                    ct=ct,
                    address=address,
                    is_failed=is_failed,
                )
                insert_rows.append(row)
                next_id += 1
                inserted += 1

    cols_sql = ", ".join(SIMPLI_INSERT_COLS)
    placeholders = ", ".join(["?"] * len(SIMPLI_INSERT_COLS))
    sql = f"INSERT INTO fpoc_simpli_visits ({cols_sql}) VALUES ({placeholders})"
    cur.executemany(sql, insert_rows)
    cn.commit()
    print(f"[seed-regiones] +{inserted} visitas en regiones (1 vez por día)")
    return inserted


def _insert_visitas_estacionalidad(cn) -> int:
    """Genera picos Black Friday + Cyber Week. Detecta el rango de fechas del
    dataset y, si BF cae dentro, agrega volumen × multiplicador. Si BF cae
    fuera, agrega ~3 semanas con BF embebido (modo simulación)."""
    cur = cn.cursor()
    cur.execute("SELECT MIN(planned_date), MAX(planned_date) FROM fpoc_simpli_visits")
    r = cur.fetchone()
    if not r or not r[0]:
        return 0
    min_d = date.fromisoformat(str(r[0])[:10])
    max_d = date.fromisoformat(str(r[1])[:10])
    print(f"[seed-bf] rango actual del dataset: {min_d} -> {max_d}")

    # Marcador de idempotencia: BF se inserta en noviembre. Si ya hay >100
    # visitas en noviembre del año correspondiente → skip.
    nov_year = max_d.year
    cur.execute(
        "SELECT COUNT(*) FROM fpoc_simpli_visits WHERE planned_date >= ? AND planned_date <= ?",
        f"{nov_year}-11-15", f"{nov_year}-12-05",
    )
    n_existing_bf = int(cur.fetchone()[0])
    if n_existing_bf > 100:
        print(f"[seed-bf] ya hay {n_existing_bf} visitas en BF window {nov_year}, skip")
        return 0

    # Determinar qué año usar para BF (preferir el rango actual; si no, año máx)
    bf_year_candidates = [max_d.year, min_d.year]
    bf_dates: list[tuple[date, float]] = []  # (fecha, multiplicador)
    for y in bf_year_candidates:
        bf = _last_friday_of_november(y)
        cyber_mon = bf + timedelta(days=3)
        # Cyber Week: Lun-Mié antes del BF
        cw_lun = bf - timedelta(days=4)
        cw_mar = bf - timedelta(days=3)
        cw_mie = bf - timedelta(days=2)
        thu_before = bf - timedelta(days=1)
        days_set = [
            (cw_lun, 2.0),
            (cw_mar, 2.0),
            (cw_mie, 2.0),
            (thu_before, 1.5),
            (bf, 3.0),
            (bf + timedelta(days=1), 1.5),  # sábado post-BF
            (cyber_mon, 2.5),
        ]
        # Si el BF está dentro o cerca del rango (max_d + 30 días buffer):
        if min_d - timedelta(days=30) <= bf <= max_d + timedelta(days=180):
            bf_dates = days_set
            print(f"[seed-bf] usando BF de {y}: {bf}")
            break

    if not bf_dates:
        # Fallback: usar año del max_d aunque no caiga en rango — agregamos como
        # "horizonte" (max_d + algunos días)
        bf = _last_friday_of_november(max_d.year)
        cyber_mon = bf + timedelta(days=3)
        bf_dates = [
            (bf - timedelta(days=4), 2.0),
            (bf - timedelta(days=3), 2.0),
            (bf - timedelta(days=2), 2.0),
            (bf - timedelta(days=1), 1.5),
            (bf, 3.0),
            (bf + timedelta(days=1), 1.5),
            (cyber_mon, 2.5),
        ]
        print(f"[seed-bf] fallback año {max_d.year}: BF={bf}")

    # Volumen base por día (referencia: ~150 rutas RM en RM normal)
    base_rutas_per_day = 35
    rng = random.Random(11_2026)

    # Continuamos desde MAX(id) + 1 (los IDs originales son timestamps grandes)
    next_id = _next_visit_id(cn)

    insert_rows = []
    for d, mult in bf_dates:
        n_rutas = int(base_rutas_per_day * mult)
        # tasa de fallo aumentada (× 1.5) en estos días
        fail_rate = 0.075  # 5% × 1.5

        for r_idx in range(n_rutas):
            # ~70% RM, 30% regiones (densidad típica)
            in_rm = rng.random() < 0.70
            if in_rm:
                comuna = rng.choice(RM_COMUNAS[:15])
                region = "RM"
                ct = rng.choice(["CD NORTE", "CD SUR", "CD OMNICANAL LOF2"])
                addr_tpl = "{street} {n} {comuna}"
                streets = ["Av. Vicuña Mackenna", "Gran Avenida", "Av. Santa Rosa",
                           "Av. Los Pajaritos", "Av. Las Industrias", "Camino a Melipilla"]
                addr_template_used = lambda: addr_tpl.format(
                    street=rng.choice(streets), n=rng.randint(100, 9999), comuna=comuna,
                )
            else:
                comuna, region, ct, addr_templates = rng.choice(REGIONES_DATA)
                addr_template_used = lambda: rng.choice(addr_templates).format(n=rng.randint(10, 9999)) + f" {comuna}"

            drv = rng.choice(DRIVER_POOL_REGIONES)
            patente = rng.randint(1, 99)
            empresa_id = rng.choice(EMPRESAS_VALIDAS)
            fecha_inicio = datetime.combine(d, datetime.min.time()) + \
                timedelta(hours=rng.randint(6, 10), minutes=rng.randint(0, 59))

            # Densidad ↑ en BF (más visitas/ruta)
            n_visitas = rng.randint(5, int(8 * mult))
            for ord_n in range(1, n_visitas + 1):
                is_failed = rng.random() < fail_rate
                row = _gen_visita_regiones(
                    visit_id=next_id,
                    planned_date=d,
                    ruta_id="",
                    order=ord_n,
                    empresa_id=empresa_id,
                    drv=drv,
                    patente=patente,
                    fecha_inicio=fecha_inicio,
                    comuna=comuna,
                    region=region,
                    ct=ct,
                    address=addr_template_used(),
                    is_failed=is_failed,
                )
                insert_rows.append(row)
                next_id += 1

    if not insert_rows:
        return 0

    cols_sql = ", ".join(SIMPLI_INSERT_COLS)
    placeholders = ", ".join(["?"] * len(SIMPLI_INSERT_COLS))
    sql = f"INSERT INTO fpoc_simpli_visits ({cols_sql}) VALUES ({placeholders})"
    cur.executemany(sql, insert_rows)
    cn.commit()
    print(f"[seed-bf] +{len(insert_rows)} visitas Black Friday/Cyber Week")
    return len(insert_rows)


# =============================================================================
# Main
# =============================================================================
def main() -> int:
    print(f"[seed-regiones-estacionalidad] backend={backend()}")
    with get_conn() as cn:
        n_cols = _add_columns_idempotent(cn)
        n_back_geo = _backfill_region_comuna(cn)
        n_regiones = _insert_visitas_regiones(cn)
        n_bf = _insert_visitas_estacionalidad(cn)
        # Backfill ruta_id incluye las nuevas visitas (que se insertan con ruta_id='')
        n_rutas = _backfill_ruta_id(cn)

        cur = cn.cursor()
        cur.execute("SELECT COUNT(*) FROM fpoc_simpli_visits")
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT region, COUNT(*) FROM fpoc_simpli_visits GROUP BY region ORDER BY 2 DESC"
        )
        print("[summary] distribución por region:")
        for r in cur.fetchall():
            print(f"   {r[0]!r}: {r[1]}")

        print(
            f"[seed-regiones] +{n_regiones} visitas regiones, +{n_bf} visitas "
            f"Black Friday/Cyber Week, +{n_cols} columnas geo. Total dataset: {total}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

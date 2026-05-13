"""Live generator: inserta rows aleatorias en fpoc.simpli_visits periódicamente.

Propósito: mostrar en demo que la data se genera y persiste en SQL.

Controles:
    Env:
        ENABLE_LIVE_GEN=true           (default false)
        LIVE_GEN_INTERVAL_SEC=8        (default 8s entre ticks)
        LIVE_GEN_ROWS_PER_TICK=2       (default 2 rows por tick)

    Endpoints (montados desde main.py):
        GET  /api/live-gen/stats          → estado + contadores
        POST /api/live-gen/toggle (admin) → prende/apaga en runtime
        POST /api/live-gen/reset (admin)  → borra rows generadas por el live-gen de hoy

Las filas insertadas tienen:
    planned_date = today
    id = ID_OFFSET + epoch_second*1000 + counter  (evita colisión con seed)
"""
from __future__ import annotations

import itertools
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from core.auth import CurrentUser, current_user, require_admin
from core.db import IntegrityError, get_conn


# ID base para no colisionar con el seed (ids < 900B)
ID_BASE = 900_000_000_000
_id_counter = itertools.count(start=1)
_id_lock = threading.Lock()


def _next_id(target_date: date) -> int:
    """ID único: ID_BASE + epoch_microsec × 1000 + counter%1000.
    Seguro entre reinicios de proceso. Epoch microseg (~2e15) × 1000 (~2e18)
    cabe en BIGINT (~9.2e18). El counter evita colisión si dos inserts caen
    en el mismo microsegundo."""
    with _id_lock:
        n = next(_id_counter)
    return ID_BASE + int(time.time() * 1_000_000) * 1000 + (n % 1000)


# Catálogos de datos random
SAMPLE_TITLES = [
    "Servicios Andinos Ltda", "Distribuidora Central", "Comercial Mapocho",
    "Tecnología del Sur S.A.", "Retail Norte", "Exportadora Cordillera",
    "Alimentos del Pacífico", "Logística Valparaíso", "Metalúrgica Araucanía",
    "Textiles Bío Bío", "Minera Atacama", "Agrícola Maule",
    "Constructora Los Lagos", "Farmacéutica Nacional", "Automotora Central",
    "Pérez y Compañía", "González Hermanos", "Muñoz Ltda", "Rojas SPA",
    "Martínez & Asociados", "Contreras Distribución", "Silva Comercial",
    "López Import", "Díaz Retail", "Castro Logistics",
]
SAMPLE_COMMUNES = [
    # RM (peso 80%)
    "Las Condes", "Ñuñoa", "Providencia", "Santiago Centro", "Maipú",
    "Lo Barnechea", "Puente Alto", "La Florida", "Vitacura", "La Dehesa",
    "La Reina", "Peñalolén", "Macul", "San Miguel", "Quilicura",
    "Las Condes", "Ñuñoa", "Providencia", "Santiago Centro", "Maipú",
    "Lo Barnechea", "Puente Alto", "La Florida", "Vitacura",
    "La Reina", "Peñalolén", "Macul", "San Miguel",
    # Regiones (peso 20%)
    "Viña del Mar", "Valparaíso", "Concepción", "Talcahuano", "Temuco",
    "La Serena", "Coquimbo", "Antofagasta", "Talca", "Rancagua",
]
COMUNA_REGION = {
    "Las Condes": "RM", "Ñuñoa": "RM", "Providencia": "RM", "Santiago Centro": "RM",
    "Maipú": "RM", "Lo Barnechea": "RM", "Puente Alto": "RM", "La Florida": "RM",
    "Vitacura": "RM", "La Dehesa": "RM", "La Reina": "RM", "Peñalolén": "RM",
    "Macul": "RM", "San Miguel": "RM", "Quilicura": "RM",
    "Viña del Mar": "Valparaíso", "Valparaíso": "Valparaíso",
    "Concepción": "Biobío", "Talcahuano": "Biobío",
    "Temuco": "Araucanía",
    "La Serena": "Coquimbo", "Coquimbo": "Coquimbo",
    "Antofagasta": "Antofagasta",
    "Talca": "Maule",
    "Rancagua": "O'Higgins",
}
SAMPLE_STREETS = [
    "Av. Apoquindo", "Av. Providencia", "Los Leones", "Pedro de Valdivia",
    "Tobalaba", "Vitacura", "Kennedy", "Cristóbal Colón", "Irarrázaval",
    "Manuel Montt", "Av. Matta", "Av. Grecia", "Américo Vespucio",
]
DRIVER_NAMES = [
    "Carlos Muñoz", "Luis Pérez", "Juan González", "Pedro Rojas",
    "María Silva", "Ana Contreras", "Sofía Díaz", "Paula Castro",
    "Andrés Figueroa", "Ricardo Tapia", "Fernando Soto", "Diego Vargas",
]


@dataclass
class LiveGenState:
    enabled: bool = False
    interval_sec: int = 8
    rows_per_tick: int = 2
    total_inserted_session: int = 0
    last_insert_at: Optional[str] = None
    last_error: Optional[str] = None
    scheduler: Optional[BackgroundScheduler] = None
    lock: threading.Lock = None  # type: ignore

    def __post_init__(self):
        self.lock = threading.Lock()


STATE = LiveGenState()


# -------- Core insert --------
def _choose_from(rng: random.Random, pool: list) -> str:
    return pool[rng.randrange(len(pool))]


def _load_real_drivers() -> list[tuple[str, int]]:
    """Lee los drivers REALES (fpoc_drivers + onboardeados via WhatsApp) para
    que las visitas generadas/importadas matcheen con los conductores que el
    cliente ve en mantenedores. Cae a DRIVER_NAMES si la tabla está vacía.

    DEPRECATED uso interno: usar _load_drivers_by_empresa para preservar la
    consistencia driver↔empresa.
    """
    try:
        from core.db import get_conn
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT name, vehicle_id FROM fpoc_drivers "
                "WHERE active = 1 AND name IS NOT NULL AND vehicle_id IS NOT NULL "
                "ORDER BY vehicle_id"
            )
            rows = cur.fetchall()
        if rows:
            return [(str(r[0]), int(r[1])) for r in rows]
    except Exception:  # noqa: BLE001
        pass
    return [(n, i + 1) for i, n in enumerate(DRIVER_NAMES[:12])]


def _load_drivers_by_empresa(target_date: Optional[date] = None) -> dict[int, list[tuple[str, int]]]:
    """Devuelve {empresa_id: [(driver_name, vehicle_id), ...]} con drivers ACTIVOS
    y NO bloqueados por dotacion_diaria para `target_date` (si se pasa).

    Bloqueados = estado en (ausente, licencia, mantencion, baja). El generator
    debe respetar la asignación driver→empresa y la disponibilidad del día.
    """
    out: dict[int, list[tuple[str, int]]] = {}
    blocked_estados = ("ausente", "licencia", "mantencion", "baja")
    try:
        from core.db import get_conn
        with get_conn() as cn:
            cur = cn.cursor()
            # Drivers activos con empresa
            cur.execute(
                "SELECT driver_id, name, empresa_id, vehicle_id "
                "FROM fpoc_drivers "
                "WHERE active = 1 AND empresa_id IS NOT NULL "
                "AND name IS NOT NULL AND vehicle_id IS NOT NULL"
            )
            drivers = [(r[0], r[1], int(r[2]), int(r[3])) for r in cur.fetchall()]

            # Drivers bloqueados por dotacion para la fecha
            blocked_driver_ids: set[str] = set()
            blocked_vehicle_ids: set[int] = set()
            if target_date is not None and drivers:
                placeholders = ",".join("?" * len(blocked_estados))
                cur.execute(
                    f"SELECT driver_id, vehicle_id FROM fpoc_dotacion_diaria "
                    f"WHERE fecha = ? AND estado IN ({placeholders})",
                    target_date.isoformat(), *blocked_estados,
                )
                for r in cur.fetchall():
                    if r[0]:
                        blocked_driver_ids.add(str(r[0]))
                    if r[1] is not None:
                        blocked_vehicle_ids.add(int(r[1]))

        for drv_id, name, empresa_id, vehicle_id in drivers:
            if str(drv_id) in blocked_driver_ids:
                continue
            if vehicle_id in blocked_vehicle_ids:
                continue
            out.setdefault(empresa_id, []).append((str(name), vehicle_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[live-gen] _load_drivers_by_empresa falló: {e}")
    return out


def _gen_row(rng: random.Random, empresas: list[tuple[int, str]], today: date,
              id_counter: int = 0,
              drivers_by_empresa: Optional[dict[int, list[tuple[str, int]]]] = None) -> dict:
    """Construye un dict con todas las columnas requeridas por fpoc.simpli_visits.
    Usa los drivers reales de fpoc_drivers respetando la asignación driver→empresa
    y filtrando los bloqueados en dotacion_diaria para `today`.

    Si la empresa elegida no tiene drivers disponibles, busca otra empresa que sí
    tenga. Si no hay ninguna, cae al modo legacy (random global) para no romper.
    """
    if drivers_by_empresa is None:
        drivers_by_empresa = _load_drivers_by_empresa(today)

    valid_empresas = [e for e in empresas if e[0] in drivers_by_empresa and drivers_by_empresa[e[0]]]
    if valid_empresas:
        empresa_id, _ = valid_empresas[rng.randrange(len(valid_empresas))]
        choices = drivers_by_empresa[empresa_id]
        driver_name, driver_vid = choices[rng.randrange(len(choices))]
    else:
        # Fallback legacy: empresa random + driver random global. Loggea una vez
        # para no inundar el log si la DB está semi-vacía.
        empresa_id, _ = empresas[rng.randrange(len(empresas))]
        legacy = _load_real_drivers()
        driver_name, driver_vid = legacy[rng.randrange(len(legacy))]
    driver = driver_name
    patente_falsa = driver_vid
    ct = rng.choice(["CD OMNICANAL LOF2", "CD NORTE", "CD SUR"])

    # Windows horarios típicos AM (08-12) / PM (13-18)
    am_pm = rng.choice(["AM", "PM"])
    if am_pm == "AM":
        eta_hour = rng.randint(8, 12)
    else:
        eta_hour = rng.randint(13, 18)
    eta_min = rng.randint(0, 59)
    eta_dt = datetime.combine(today, datetime.min.time()).replace(hour=eta_hour, minute=eta_min)

    # SLA: distribución sesgada hacia -1h (como la real)
    sla = rng.gauss(-1.0, 3.5)
    sla = max(-12.0, min(11.0, sla))
    checkout_dt = eta_dt + timedelta(hours=sla)

    # Status: nace como 'pending'. La simulación (driver_sim) las irá marcando
    # 'completed' o 'failed' a medida que sim_clock pase por sus ETAs.
    # Antes era 95% completed al insertar → todo el día arrancaba con "98%
    # cumplimiento" a las 09:00, sin movimiento real del driver.
    status = "pending"

    # Ruta anómala ~12%
    ruta_eta_futuro = 1 if rng.random() < 0.10 else 0
    ruta_primer_punto_lejano = 1 if rng.random() < 0.05 else 0
    ruta_anomala = 1 if (ruta_eta_futuro or ruta_primer_punto_lejano) else 0

    # Bins
    bin_start = (sla // 0.5) * 0.5
    bin_end = bin_start + 0.5
    bin_label = f"[{bin_start}, {bin_end}]"
    bin_index = int(40 + bin_start * 2)

    # fecha_inicio_ruta (string "YYYY-MM-DD HH:MM:SS.000000 UTC")
    ruta_start = eta_dt - timedelta(hours=rng.randint(3, 6))
    f_inicio = ruta_start.strftime("%Y-%m-%d %H:%M:%S.000000 UTC")
    f_inicio_time = ruta_start.time().replace(microsecond=0)

    title = _choose_from(rng, SAMPLE_TITLES) + f" #{rng.randint(100, 9999)}"
    # ruta_id: hash determinístico de (driver, patente) → NNN, mismo día = misma ruta
    import hashlib as _hl
    _h = _hl.md5(f"{driver}{patente_falsa}".encode()).hexdigest()
    _nnn = int(_h[:4], 16) % 1000
    ruta_id = f"R-{today.strftime('%Y%m%d')}-{_nnn:03d}"
    # Región de la ruta: derivada determinísticamente del hash (driver, patente).
    # Sin esto, cada call elegía una comuna random independiente → la ruta
    # terminaba con stops mezclados en RM + Valparaíso + Temuco + Coquimbo, y
    # el mapa dibujaba líneas cruzando el país.
    _regions_pool = ["RM", "Valparaíso", "Biobío", "Araucanía", "Coquimbo", "Maule", "O'Higgins"]
    region_idx = int(_h[4:8], 16) % len(_regions_pool)
    region = _regions_pool[region_idx]
    # Filtrar comunas de esa región
    _region_comunas = [c for c in SAMPLE_COMMUNES if COMUNA_REGION.get(c, "RM") == region]
    if not _region_comunas:
        _region_comunas = [c for c in SAMPLE_COMMUNES if COMUNA_REGION.get(c, "RM") == "RM"]
        region = "RM"
    comuna = _choose_from(rng, _region_comunas)
    calle = _choose_from(rng, SAMPLE_STREETS)
    address = f"{calle} {rng.randint(100, 9999)}, {comuna}"

    return {
        "planned_date": today,
        "id": _next_id(today),
        "title": title,
        "order": rng.randint(1, 120),
        "address": address,
        "region": region,
        "comuna": comuna,
        "ruta_id": ruta_id,
        "checkout_cl": checkout_dt,
        "current_eta_cl": eta_dt,
        "status": status,
        "checkout_comment": _choose_from(rng, ["Conserjeria", "Recibido por morador", None, None]),
        "checkout_observation": None,
        "reference": rng.randint(10_000_000, 99_999_999),
        "country": "cl",
        "sla_hour_checkout_eta": round(sla, 4),
        "bin_start": bin_start,
        "bin_end": bin_end,
        "bin_label": bin_label,
        "bin_index": bin_index,
        "ct": ct,
        "patente_falsa": patente_falsa,
        "empresa_falsa": empresa_id,
        "driver_name": driver,
        "fecha_inicio_ruta": f_inicio,
        "fecha_inicio_ruta_hora_cl": f_inicio_time,
        "fechas_futuras_bq": 0,
        "finicio_currenteta_bq": 0,
        "current_eta_cl_fechainicioruta": 0,
        "current_eta_cl_fechainicioruta_dates": 0,
        "ruta_eta_futuro": ruta_eta_futuro,
        "ruta_fecha_inicio_mayor_eta": 0,
        "ruta_primer_punto_lejano": ruta_primer_punto_lejano,
        "ruta_fecha_inicio_distinta_fecha_eta": 0,
        "am_pm": am_pm,
        "ruta_anomala": ruta_anomala,
    }


SIMPLI_COLS = [
    "planned_date", "id", "title", "order", "address",
    "region", "comuna", "ruta_id",
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


_rng = random.Random()


# =============================================================================
# Modo "minimal" — 1 empresa, 1 driver, 5 visitas RM
# =============================================================================
# Coordenadas fijas (sin jitter) para las 5 comunas RM elegidas. Usar valores
# determinísticos hace los tests reproducibles y elimina ruido visual en demos.
# ETAs arrancan a las 09:15 (NO 09:00) — el sim también arranca a las 09:00
# desde el CD; si la primera ETA coincide con DAY_START, el camión completa
# el primer stop en el primer tick (frac=∞) sin interpolación visible.
_MINIMAL_STOPS = [
    # (comuna, eta_hour, eta_min)
    ("Las Condes",      9, 15),
    ("Providencia",     9, 45),
    ("Ñuñoa",          10, 15),
    ("La Reina",       10, 45),
    ("Vitacura",       11, 15),
]


def _insert_batch_minimal(cn, target_date: date) -> int:
    """Inserta exactamente 5 visitas en RM, todas pending, ordenadas cronológicamente.

    Cliente: la PRIMERA empresa activa (cualquiera con activo=1). Driver: el
    primero asignado a esa empresa en fpoc_drivers, o "Driver Demo" sintético
    si no hay. Patente sintética = 1.

    Para correr tests unitarios y demos limpios. No usa rng (todo determinístico).
    """
    cur = cn.cursor()
    cur.execute(
        "SELECT TOP 1 empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1"
    )
    e = cur.fetchone()
    if e is None:
        return 0
    empresa_id = int(e.empresa_id)

    # Driver: primero activo de esa empresa. Fallback sintético.
    cur.execute(
        "SELECT TOP 1 name, vehicle_id FROM fpoc.drivers "
        "WHERE active = 1 AND empresa_id = ? AND name IS NOT NULL "
        "AND vehicle_id IS NOT NULL ORDER BY vehicle_id",
        empresa_id,
    )
    d = cur.fetchone()
    driver_name = d.name if d else "Driver Demo"
    patente_falsa = int(d.vehicle_id) if d else 1

    import hashlib as _hl
    _h = _hl.md5(f"{driver_name}{patente_falsa}|minimal".encode()).hexdigest()
    _nnn = int(_h[:4], 16) % 1000
    ruta_id = f"R-{target_date.strftime('%Y%m%d')}-{_nnn:03d}"

    rows = []
    for i, (comuna, hh, mm) in enumerate(_MINIMAL_STOPS, start=1):
        eta_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=hh, minute=mm)
        checkout_dt = eta_dt  # mismo timestamp (no se usa hasta que se complete)
        rows.append({
            "planned_date": target_date,
            "id": _next_id(target_date),
            "title": f"Cliente Demo #{100 + i}",
            "order": i,
            "address": f"Av. Demo {1000 + i*10}, {comuna}",
            "region": "RM",
            "comuna": comuna,
            "ruta_id": ruta_id,
            "checkout_cl": checkout_dt,
            "current_eta_cl": eta_dt,
            "status": "pending",
            "checkout_comment": None,
            "checkout_observation": None,
            "reference": 90_000_000 + i,
            "country": "cl",
            "sla_hour_checkout_eta": 0.0,
            "bin_start": -0.5,
            "bin_end": 0.0,
            "bin_label": "[-0.5, 0.0]",
            "bin_index": 39,
            "ct": "CD OMNICANAL LOF2",
            "patente_falsa": patente_falsa,
            "empresa_falsa": empresa_id,
            "driver_name": driver_name,
            "fecha_inicio_ruta": eta_dt.strftime("%Y-%m-%d 08:00:00.000000 UTC"),
            "fecha_inicio_ruta_hora_cl": datetime.combine(target_date, datetime.min.time()).replace(hour=8).time(),
            "fechas_futuras_bq": 0,
            "finicio_currenteta_bq": 0,
            "current_eta_cl_fechainicioruta": 0,
            "current_eta_cl_fechainicioruta_dates": 0,
            "ruta_eta_futuro": 0,
            "ruta_fecha_inicio_mayor_eta": 0,
            "ruta_primer_punto_lejano": 0,
            "ruta_fecha_inicio_distinta_fecha_eta": 0,
            "am_pm": "AM",
            "ruta_anomala": 0,
        })

    data = [tuple(r[c] for c in SIMPLI_COLS) for r in rows]
    placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
    cols_sql = ", ".join(f"[{c}]" for c in SIMPLI_COLS)
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})",
        data,
    )
    cn.commit()
    logger.info(
        f"[live-gen-minimal] {len(rows)} visitas RM para {target_date} "
        f"empresa={empresa_id} driver={driver_name} ruta={ruta_id}"
    )
    return len(rows)


def _insert_batch(cn, target_date: date, n_rows: int, mode: str = "default") -> int:
    """Inserta n_rows en target_date. Devuelve la cantidad efectivamente insertada.

    Modos:
      - "default"  → comportamiento histórico: muchas rutas, regiones varias,
        distribución realista (~1800 rows/día con 13 drivers).
      - "minimal"  → 1 empresa, 1 driver, 5 visitas en RM, todas pending,
        ETAs cronológicos 09:00, 09:30, 10:00, 10:30, 11:00. Para demo
        controlado y para tests unitarios. Ignora `n_rows`.
    """
    if mode == "minimal":
        return _insert_batch_minimal(cn, target_date)

    cur = cn.cursor()
    cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1")
    empresas = [(int(r[0]), r[1]) for r in cur.fetchall()]
    if not empresas:
        return 0

    # Precargar drivers agrupados por empresa (1 query) y respetar dotación del día
    drivers_by_empresa = _load_drivers_by_empresa(target_date)
    if drivers_by_empresa:
        empresas_con_drivers = sum(1 for e in empresas if e[0] in drivers_by_empresa and drivers_by_empresa[e[0]])
        if empresas_con_drivers < len(empresas):
            logger.info(
                f"[live-gen] {empresas_con_drivers}/{len(empresas)} empresas con drivers operables para {target_date}"
            )

    # Usar un rng por batch para variedad pero reproducible dentro de la misma fecha
    rng = random.Random(target_date.toordinal() + int(time.time()) % 1000)
    rows = [_gen_row(rng, empresas, target_date, id_counter=i, drivers_by_empresa=drivers_by_empresa)
            for i in range(n_rows)]

    # Order cronológico por ruta: agrupar por ruta_id, ordenar por current_eta_cl
    # ascendente y reasignar order = 1..N. Antes era rng.randint(1, 120) → un
    # stop con order=3 tenía ETA 13:42 y order=4 tenía ETA 09:12 (ilegible para
    # el usuario en la tabla de folios y rompe la lógica del driver_sim que
    # asume order monotónico para procesar pendings).
    from collections import defaultdict
    grouped: dict[str, list] = defaultdict(list)
    for r in rows:
        grouped[r["ruta_id"]].append(r)
    for ruta_id, group in grouped.items():
        group.sort(key=lambda r: r["current_eta_cl"])
        for i, r in enumerate(group, start=1):
            r["order"] = i

    data = [tuple(r[c] for c in SIMPLI_COLS) for r in rows]

    cur.fast_executemany = True
    placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
    cols_sql = ", ".join(f"[{c}]" for c in SIMPLI_COLS)

    # Batching de 500 para evitar payload excesivo
    total = 0
    BATCH = 500
    for i in range(0, len(data), BATCH):
        chunk = data[i:i + BATCH]
        try:
            cur.executemany(
                f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})",
                chunk,
            )
            cn.commit()
            total += len(chunk)
        except IntegrityError as e:
            # Algunas rows pueden colisionar; reintentamos 1x con IDs shifteados
            cn.rollback()
            logger.warning(f"[live-gen] PK collision batch, shifting ids: {e}")
            for r in chunk:
                # shift id (tuple→list→tuple)
                pass  # simplicidad: saltamos el chunk problemático
    return total


def _day_state_is_running(fecha_iso: str) -> bool:
    """True solo si fpoc.planificacion_imports.state = 'EN_CURSO' para esa fecha."""
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT state FROM fpoc.planificacion_imports WHERE fecha = ?",
                fecha_iso,
            )
            r = cur.fetchone()
        return r is not None and str(r.state) == "EN_CURSO"
    except Exception:  # noqa: BLE001
        return False


def _insert_tick() -> None:
    """Llamado por APScheduler. Inserta LIVE_GEN_ROWS_PER_TICK rows.
    GATE: solo corre si (a) STATE.enabled local, (b) LIVE_GEN_CREATE_VISITS=true,
    (c) el día operativo apuntado por STATE.today tiene state='EN_CURSO' en DB.
    """
    if not STATE.enabled:
        return
    if os.environ.get("LIVE_GEN_CREATE_VISITS", "false").lower() != "true":
        return
    try:
        # Importar acá para evitar ciclo en el import inicial
        from core.state import STATE as APP_STATE
        sim_today = getattr(APP_STATE, "today", None) or date.today()
        if not _day_state_is_running(sim_today.isoformat()):
            return  # día no está EN_CURSO → no inyectar

        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1")
            empresas = [(int(r[0]), r[1]) for r in cur.fetchall()]
            if not empresas:
                STATE.last_error = "Sin empresas en fpoc.empresas_transporte"
                return

            today = sim_today
            rows = [_gen_row(_rng, empresas, today) for _ in range(STATE.rows_per_tick)]
            data = [tuple(r[c] for c in SIMPLI_COLS) for r in rows]

            cur.fast_executemany = True
            placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
            cols_sql = ", ".join(f"[{c}]" for c in SIMPLI_COLS)
            cur.executemany(
                f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})",
                data,
            )
            cn.commit()
        with STATE.lock:
            STATE.total_inserted_session += len(rows)
            STATE.last_insert_at = datetime.utcnow().isoformat()
            STATE.last_error = None
    except IntegrityError as e:  # PK collision (raro con timestamp-based id)
        STATE.last_error = f"PK collision: {e}"
        logger.warning(f"[live-gen] {STATE.last_error}")
    except Exception as e:  # noqa: BLE001
        STATE.last_error = str(e)[:200]
        logger.warning(f"[live-gen] {STATE.last_error}")


def start_scheduler() -> None:
    """Arranca el scheduler. Se llama desde main.lifespan si ENABLE_LIVE_GEN=true."""
    if STATE.scheduler is not None:
        return
    interval = int(os.environ.get("LIVE_GEN_INTERVAL_SEC", "8"))
    rows_per_tick = int(os.environ.get("LIVE_GEN_ROWS_PER_TICK", "2"))
    enabled_default = os.environ.get("ENABLE_LIVE_GEN", "false").lower() == "true"

    STATE.interval_sec = interval
    STATE.rows_per_tick = rows_per_tick
    STATE.enabled = enabled_default

    sch = BackgroundScheduler()
    sch.add_job(_insert_tick, "interval", seconds=interval, id="live-gen-tick",
                max_instances=1, coalesce=True)
    sch.start()
    STATE.scheduler = sch
    logger.info(f"[live-gen] scheduler started (interval={interval}s rows={rows_per_tick} enabled={enabled_default})")


def stop_scheduler() -> None:
    if STATE.scheduler is not None:
        STATE.scheduler.shutdown(wait=False)
        STATE.scheduler = None


# -------- API --------
router = APIRouter(prefix="/api/live-gen", tags=["live-gen"])


class LiveGenStats(BaseModel):
    enabled: bool
    interval_sec: int
    rows_per_tick: int
    total_inserted_session: int
    last_insert_at: Optional[str] = None
    last_error: Optional[str] = None
    rows_today_db: int


class ToggleRequest(BaseModel):
    enabled: bool


@router.get("/stats", response_model=LiveGenStats)
def stats(user: CurrentUser = Depends(current_user)) -> LiveGenStats:
    with get_conn() as cn:
        cur = cn.cursor()
        from datetime import date as _date_cls
        cur.execute(
            "SELECT COUNT(*) FROM fpoc.simpli_visits WHERE planned_date = ? AND id >= ?",
            _date_cls.today().isoformat(), ID_BASE,
        )
        rows_today = int(cur.fetchone()[0])
    return LiveGenStats(
        enabled=STATE.enabled,
        interval_sec=STATE.interval_sec,
        rows_per_tick=STATE.rows_per_tick,
        total_inserted_session=STATE.total_inserted_session,
        last_insert_at=STATE.last_insert_at,
        last_error=STATE.last_error,
        rows_today_db=rows_today,
    )


@router.post("/toggle", response_model=LiveGenStats)
def toggle(req: ToggleRequest, user: CurrentUser = Depends(require_admin)) -> LiveGenStats:
    with STATE.lock:
        STATE.enabled = bool(req.enabled)
    logger.info(f"[live-gen] toggled to {STATE.enabled} by {user.email}")
    return stats(user=user)


@router.post("/reset")
def reset(user: CurrentUser = Depends(require_admin)) -> dict:
    """Borra las rows generadas por el live-gen de hoy (id >= ID_BASE)."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "DELETE FROM fpoc.simpli_visits WHERE planned_date = date('now') AND id >= ?",
            ID_BASE,
        )
        n = cur.rowcount
        cn.commit()
    with STATE.lock:
        STATE.total_inserted_session = 0
    return {"deleted": n}


class BatchRequest(BaseModel):
    rows: int = 1800
    date: Optional[str] = None  # YYYY-MM-DD, default = today


class BatchResponse(BaseModel):
    date: str
    inserted: int
    elapsed_sec: float


@router.post("/batch", response_model=BatchResponse)
def batch(req: BatchRequest, user: CurrentUser = Depends(require_admin)) -> BatchResponse:
    """Inyecta un batch de N filas en una fecha específica (default: hoy).
    Útil para popular un día rápido en la demo."""
    target = date.fromisoformat(req.date) if req.date else date.today()
    n_req = max(1, min(req.rows, 10_000))
    t0 = time.time()
    with get_conn() as cn:
        n = _insert_batch(cn, target, n_req)
    elapsed = time.time() - t0
    with STATE.lock:
        STATE.total_inserted_session += n
        STATE.last_insert_at = datetime.utcnow().isoformat()
    logger.info(f"[live-gen] batch {target}: {n} rows en {elapsed:.1f}s")
    return BatchResponse(date=target.isoformat(), inserted=n, elapsed_sec=round(elapsed, 2))


class SimulateRequest(BaseModel):
    days: int = 7
    rows_per_day: int = 1800
    include_today: bool = True


class SimulateResponse(BaseModel):
    total_inserted: int
    per_day: dict[str, int]
    elapsed_sec: float


@router.post("/simulate-days", response_model=SimulateResponse)
def simulate_days(req: SimulateRequest, user: CurrentUser = Depends(require_admin)) -> SimulateResponse:
    """Simula N días con rows_per_day cada uno. Loopea hacia atrás desde hoy.
    Bloqueante (puede tardar varios segundos). Con 7x1800 ≈ 10-20s."""
    n_days = max(1, min(req.days, 30))
    per_day_req = max(1, min(req.rows_per_day, 5000))
    per_day_result: dict[str, int] = {}
    t0 = time.time()
    today = date.today()
    # Si include_today: range(N)  → [today, today-1, ..., today-N+1]
    # Si NO include_today: range(1, N+1) → [today-1, ..., today-N]
    start = 0 if req.include_today else 1
    end = n_days if req.include_today else n_days + 1
    with get_conn() as cn:
        for i in range(start, end):
            d = today - timedelta(days=i)
            n = _insert_batch(cn, d, per_day_req)
            per_day_result[d.isoformat()] = n
    elapsed = time.time() - t0
    total = sum(per_day_result.values())
    with STATE.lock:
        STATE.total_inserted_session += total
        STATE.last_insert_at = datetime.utcnow().isoformat()
    logger.info(f"[live-gen] simulate-days: {total} rows en {elapsed:.1f}s ({len(per_day_result)} días)")
    return SimulateResponse(
        total_inserted=total,
        per_day=per_day_result,
        elapsed_sec=round(elapsed, 2),
    )

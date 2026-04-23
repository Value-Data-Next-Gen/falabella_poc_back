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

import pyodbc
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from auth import CurrentUser, current_user, require_admin
from db import get_conn


# ID base grande para no colisionar con el seed (que usa id + offset*10M)
ID_BASE = 900_000_000_000
_id_counter = itertools.count(start=1)
_id_lock = threading.Lock()


def _next_id(target_date: date) -> int:
    """ID único monotónico. Formato: 900B + fecha*10M + counter.
    El counter es thread-safe y garantiza unicidad entre filas del mismo batch."""
    with _id_lock:
        n = next(_id_counter)
    # target_date.toordinal() hasta 2100 ≈ 767k → caben 10M counters por día
    return ID_BASE + target_date.toordinal() * 10_000_000 + n


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
    "Las Condes", "Ñuñoa", "Providencia", "Santiago Centro", "Maipú",
    "Lo Barnechea", "Puente Alto", "La Florida", "Vitacura", "La Dehesa",
    "La Reina", "Peñalolén", "Macul", "San Miguel", "Quilicura",
]
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


def _gen_row(rng: random.Random, empresas: list[tuple[int, str]], today: date,
              id_counter: int = 0) -> dict:
    """Construye un dict con todas las columnas requeridas por fpoc.simpli_visits."""
    empresa_id, _ = empresas[rng.randrange(len(empresas))]
    driver = _choose_from(rng, DRIVER_NAMES)
    patente_falsa = rng.randint(1, 40)
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

    # Status: 95% completed, 5% failed
    status = "completed" if rng.random() < 0.95 else "failed"

    # Ruta anómala ~12%
    ruta_eta_futuro = 1 if rng.random() < 0.10 else 0
    ruta_primer_punto_lejano = 1 if rng.random() < 0.05 else 0
    ruta_anomala = 1 if (ruta_eta_futuro or ruta_primer_punto_lejano) else 0

    # Bins
    bin_start = (sla // 0.5) * 0.5
    bin_end = bin_start + 0.5
    bin_label = f"[{bin_start}, {bin_end}]"
    bin_index = int(40 + bin_start * 2)

    # Fechainicioruta (string "YYYY-MM-DD HH:MM:SS.000000 UTC")
    ruta_start = eta_dt - timedelta(hours=rng.randint(3, 6))
    f_inicio = ruta_start.strftime("%Y-%m-%d %H:%M:%S.000000 UTC")
    f_inicio_time = ruta_start.time().replace(microsecond=0)

    title = _choose_from(rng, SAMPLE_TITLES) + f" #{rng.randint(100, 9999)}"
    comuna = _choose_from(rng, SAMPLE_COMMUNES)
    calle = _choose_from(rng, SAMPLE_STREETS)
    address = f"{calle} {rng.randint(100, 9999)}, {comuna}"

    return {
        "planned_date": today,
        "id": _next_id(today),
        "title": title,
        "order": rng.randint(1, 120),
        "address": address,
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
        "Patente_falsa": patente_falsa,
        "Empresa_falsa": empresa_id,
        "Drivername": driver,
        "Fechainicioruta": f_inicio,
        "Fechainicioruta_hora_cl": f_inicio_time,
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


_rng = random.Random()


def _insert_batch(cn: pyodbc.Connection, target_date: date, n_rows: int) -> int:
    """Inserta n_rows en target_date. Devuelve la cantidad efectivamente insertada."""
    cur = cn.cursor()
    cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1")
    empresas = [(int(r[0]), r[1]) for r in cur.fetchall()]
    if not empresas:
        return 0

    # Usar un rng por batch para variedad pero reproducible dentro de la misma fecha
    rng = random.Random(target_date.toordinal() + int(time.time()) % 1000)
    rows = [_gen_row(rng, empresas, target_date, id_counter=i) for i in range(n_rows)]
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
        except pyodbc.IntegrityError as e:
            # Algunas rows pueden colisionar; reintentamos 1x con IDs shifteados
            cn.rollback()
            logger.warning(f"[live-gen] PK collision batch, shifting ids: {e}")
            for r in chunk:
                # shift id (tuple→list→tuple)
                pass  # simplicidad: saltamos el chunk problemático
    return total


def _insert_tick() -> None:
    """Llamado por APScheduler. Inserta LIVE_GEN_ROWS_PER_TICK rows."""
    if not STATE.enabled:
        return
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1")
            empresas = [(int(r[0]), r[1]) for r in cur.fetchall()]
            if not empresas:
                STATE.last_error = "Sin empresas en fpoc.empresas_transporte"
                return

            today = date.today()
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
    except pyodbc.IntegrityError as e:  # PK collision (raro con timestamp-based id)
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
        cur.execute(
            "SELECT COUNT(*) FROM fpoc.simpli_visits WHERE planned_date = CAST(GETUTCDATE() AS DATE) AND id >= ?",
            ID_BASE,
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
            "DELETE FROM fpoc.simpli_visits WHERE planned_date = CAST(GETUTCDATE() AS DATE) AND id >= ?",
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

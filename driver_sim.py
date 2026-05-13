"""Simulador de movimiento de drivers + entregas en tiempo real.

Para cada ruta en estado EN_CURSO:
  1. Avanza un reloj simulado (sim_clock por fecha)
  2. Interpola la posición del driver entre stop actual y próximo
  3. Cuando sim_clock alcanza la ETA del próximo stop, lo marca completed
     (95% prob) o failed (5% con motivo random del catálogo)
  4. Actualiza fpoc.driver_positions con la pos snapshot

Configurable:
  - SIM_TICK_SEC: intervalo real entre ticks (default 10s)
  - SIM_MINUTES_PER_TICK: minutos sim avanzados por tick (default 30 → 1h
    de operación cada 2 min real)

Endpoint:
  GET /api/operacion/driver-positions?fecha=YYYY-MM-DD
      Lista las posiciones actuales por driver para el mapa.

Trigger:
  Al transition VALIDADO → EN_CURSO, day_state.py llama start_sim(fecha).
  Al CERRADO, llama stop_sim(fecha).
"""
from __future__ import annotations

import math
import os
import random
import threading
from datetime import date as _date_cls, datetime, time as _time, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from auth import CurrentUser, current_user
from db import get_conn


router = APIRouter(prefix="/api/operacion", tags=["driver-sim"])


SIM_TICK_SEC = int(os.environ.get("SIM_TICK_SEC", "10"))
SIM_MINUTES_PER_TICK = int(os.environ.get("SIM_MINUTES_PER_TICK", "30"))
FAIL_PROBABILITY = float(os.environ.get("SIM_FAIL_PROB", "0.05"))
DELIVERY_DELAY_PROB = float(os.environ.get("SIM_DELAY_PROB", "0.15"))
DAY_START = _time(9, 0)
DAY_END = _time(20, 0)

# Motivos catálogo para entregas fallidas (mismo set del clasificador)
FAILURE_REASONS = [
    "SIN MORADORES",
    "NO CONOCEN A CLIENTE",
    "CLIENTE RECHAZA",
    "PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN",
    "FUERA DE COBERTURA/ FRECUENCIA",
]

# Centroides aproximados (lat, lon) por comuna. Para simulación visual del mapa.
# El XLSX no trae coordenadas de cada visita; usamos centroide + jitter.
COMUNA_CENTROIDS = {
    # RM
    "Santiago": (-33.4489, -70.6693),
    "Santiago Centro": (-33.4489, -70.6693),
    "Las Condes": (-33.4137, -70.5807),
    "Providencia": (-33.4313, -70.6093),
    "Ñuñoa": (-33.4565, -70.5944),
    "Maipú": (-33.5111, -70.7580),
    "Puente Alto": (-33.6111, -70.5755),
    "La Florida": (-33.5226, -70.5984),
    "Vitacura": (-33.3892, -70.5734),
    "Lo Barnechea": (-33.3517, -70.5169),
    "La Reina": (-33.4474, -70.5400),
    "Peñalolén": (-33.4860, -70.5333),
    "Peñalolen": (-33.4860, -70.5333),
    "Macul": (-33.4860, -70.5944),
    "San Miguel": (-33.4986, -70.6543),
    "Quilicura": (-33.3589, -70.7290),
    "San Bernardo": (-33.5917, -70.7000),
    "La Cisterna": (-33.5325, -70.6644),
    "El Bosque": (-33.5614, -70.6743),
    "La Pintana": (-33.5878, -70.6342),
    "Independencia": (-33.4189, -70.6645),
    "Recoleta": (-33.4070, -70.6440),
    "Estación Central": (-33.4569, -70.6886),
    "Cerrillos": (-33.4953, -70.7106),
    "Pudahuel": (-33.4400, -70.7700),
    "Renca": (-33.4081, -70.7264),
    "Lo Espejo": (-33.5256, -70.6878),
    "San Joaquín": (-33.4956, -70.6308),
    "Huechuraba": (-33.3625, -70.6403),
    "Conchalí": (-33.3837, -70.6700),
    "Quinta Normal": (-33.4286, -70.7000),
    "Lo Prado": (-33.4444, -70.7261),
    "Cerro Navia": (-33.4192, -70.7375),
    "Pedro Aguirre Cerda": (-33.4892, -70.6711),
    # Regiones
    "Valparaíso": (-33.0472, -71.6127),
    "Viña del Mar": (-33.0153, -71.5500),
    "Concepción": (-36.8201, -73.0444),
    "Talcahuano": (-36.7250, -73.1153),
    "Temuco": (-38.7359, -72.5904),
    "La Serena": (-29.9027, -71.2519),
    "Coquimbo": (-29.9534, -71.3436),
    "Antofagasta": (-23.6500, -70.4000),
    "Talca": (-35.4264, -71.6553),
    "Rancagua": (-34.1708, -70.7444),
    "Curicó": (-34.9847, -71.2394),
}
DEFAULT_LATLON = (-33.4489, -70.6693)  # Santiago


def _comuna_latlon(comuna: Optional[str], rng: random.Random) -> tuple[float, float]:
    """Devuelve (lat, lon) con jitter para una comuna."""
    base = COMUNA_CENTROIDS.get((comuna or "").strip().title(), DEFAULT_LATLON)
    # Jitter ±0.01° (~1 km)
    return (base[0] + rng.uniform(-0.01, 0.01),
            base[1] + rng.uniform(-0.01, 0.01))


# ============================================================================
# Estado del simulador
# ============================================================================
class SimState:
    """Estado en memoria. Una fecha simulándose a la vez. sim_clock por fecha."""
    scheduler: Optional[BackgroundScheduler] = None
    active_dates: dict[str, datetime] = {}  # fecha_iso -> sim_clock
    lock = threading.Lock()


_STATE = SimState()


def start_sim(fecha_iso: str) -> None:
    """Llamado al pasar VALIDADO → EN_CURSO. Inicializa sim_clock = 09:00 del día."""
    with _STATE.lock:
        if fecha_iso in _STATE.active_dates:
            logger.info(f"[driver-sim] ya simulando {fecha_iso}, no re-inicio")
            return
        try:
            day = _date_cls.fromisoformat(fecha_iso)
        except ValueError:
            logger.warning(f"[driver-sim] fecha inválida: {fecha_iso}")
            return
        _STATE.active_dates[fecha_iso] = datetime.combine(day, DAY_START)
        logger.info(f"[driver-sim] arrancada simulación de {fecha_iso} desde 09:00")
        # Snapshot inicial de posiciones (en depots / primer stop)
        try:
            _init_positions(fecha_iso)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[driver-sim] init falló: {e}")


def stop_sim(fecha_iso: str) -> None:
    """Llamado al CERRADO. Limpia el estado en memoria; positions quedan en DB."""
    with _STATE.lock:
        _STATE.active_dates.pop(fecha_iso, None)
        logger.info(f"[driver-sim] detenida simulación de {fecha_iso}")


def start_scheduler() -> None:
    """Llamado desde main.lifespan."""
    if _STATE.scheduler is not None:
        return
    sch = BackgroundScheduler()
    sch.add_job(_tick, "interval", seconds=SIM_TICK_SEC,
                id="driver-sim-tick", max_instances=1, coalesce=True)
    sch.start()
    _STATE.scheduler = sch
    logger.info(f"[driver-sim] scheduler arrancado (tick={SIM_TICK_SEC}s, +{SIM_MINUTES_PER_TICK}min sim/tick)")


def stop_scheduler() -> None:
    if _STATE.scheduler:
        _STATE.scheduler.shutdown(wait=False)
        _STATE.scheduler = None


# ============================================================================
# Helpers
# ============================================================================
def _init_positions(fecha_iso: str) -> None:
    """Crea row inicial en driver_positions por cada ruta del día."""
    with get_conn() as cn:
        cur = cn.cursor()
        # Limpiar positions previas del día (si arranca un re-run)
        cur.execute(
            "DELETE FROM fpoc.driver_positions WHERE planned_date = ?", fecha_iso,
        )
        # Una row por vehicle_id (patente) del día.
        # NOTA: el XLSX de SimpliRoute no trae lat/lon — usamos centroide de
        # la comuna del primer stop, con jitter, para el render del mapa.
        cur.execute(
            """SELECT DISTINCT patente_falsa, ruta_id, driver_name
               FROM fpoc.simpli_visits
               WHERE planned_date = ? AND ruta_id IS NOT NULL""",
            fecha_iso,
        )
        rutas = cur.fetchall()
        rng = random.Random(hash(fecha_iso))
        for r in rutas:
            if r.patente_falsa is None:
                continue
            cur.execute(
                """SELECT TOP 1 comuna FROM fpoc.simpli_visits
                   WHERE patente_falsa = ? AND planned_date = ?
                   ORDER BY [order]""",
                int(r.patente_falsa), fecha_iso,
            )
            cm_row = cur.fetchone()
            comuna = cm_row.comuna if cm_row else None
            lat, lon = _comuna_latlon(comuna, rng)
            cur.execute(
                """INSERT INTO fpoc.driver_positions
                   (vehicle_id, planned_date, ruta_id, driver_name, patente_falsa,
                    current_stop, next_stop, lat, lon, status, speed_kmh)
                   VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?, 'en_ruta', 25)""",
                int(r.patente_falsa), fecha_iso,
                r.ruta_id, r.driver_name, int(r.patente_falsa),
                lat, lon,
            )
        cn.commit()
        logger.info(f"[driver-sim] {len(rutas)} drivers inicializados para {fecha_iso}")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en km entre 2 puntos lat/lon."""
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _tick() -> None:
    """Avanza sim_clock para cada fecha activa y procesa rutas EN_CURSO."""
    if not _STATE.active_dates:
        return
    try:
        with _STATE.lock:
            dates_snapshot = list(_STATE.active_dates.items())
        for fecha_iso, sim_clock in dates_snapshot:
            new_clock = sim_clock + timedelta(minutes=SIM_MINUTES_PER_TICK)
            # Cap al fin de jornada
            end_dt = datetime.combine(sim_clock.date(), DAY_END)
            if new_clock > end_dt:
                new_clock = end_dt
            _process_date(fecha_iso, new_clock)
            with _STATE.lock:
                _STATE.active_dates[fecha_iso] = new_clock
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[driver-sim] tick error: {e}")


def _process_date(fecha_iso: str, sim_clock: datetime) -> None:
    """Procesa una fecha: avanza drivers y completa stops vencidos."""
    with get_conn() as cn:
        cur = cn.cursor()
        # Listar drivers activos
        cur.execute(
            "SELECT vehicle_id, ruta_id, current_stop, next_stop, lat, lon, status "
            "FROM fpoc.driver_positions "
            "WHERE planned_date = ? AND status NOT IN ('finalizado')",
            fecha_iso,
        )
        drivers = cur.fetchall()
        rng = random.Random()
        for d in drivers:
            vehicle_id = int(d.vehicle_id)
            if d.status == 'finalizado':
                continue
            # Buscar el próximo stop pendiente.
            # simpli_visits NO tiene lat/lon — derivamos del centroide de comuna.
            cur.execute(
                """SELECT TOP 1 id, [order], comuna, current_eta_cl
                   FROM fpoc.simpli_visits
                   WHERE patente_falsa = ? AND planned_date = ?
                     AND status = 'pending'
                   ORDER BY [order]""",
                vehicle_id, fecha_iso,
            )
            ns = cur.fetchone()
            if ns is None:
                # Driver terminó la ruta
                cur.execute(
                    "UPDATE fpoc.driver_positions SET status = 'finalizado', "
                    "speed_kmh = 0, ts_sim = ?, updated_at = SYSDATETIME() "
                    "WHERE vehicle_id = ? AND planned_date = ?",
                    sim_clock, vehicle_id, fecha_iso,
                )
                cn.commit()
                continue

            next_stop_id = int(ns.id)
            next_order = int(ns.order) if ns.order is not None else 0
            target_lat, target_lon = _comuna_latlon(ns.comuna, rng)
            try:
                eta_str = str(ns.current_eta_cl)
                eta = datetime.strptime(eta_str[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:  # noqa: BLE001
                eta = sim_clock  # forzamos resolución si ETA inválido

            # ¿Llegó al stop?
            if sim_clock >= eta and target_lat and target_lon:
                # Resolver stop: 95% completed, 5% failed
                if rng.random() < FAIL_PROBABILITY:
                    motivo = rng.choice(FAILURE_REASONS)
                    cur.execute(
                        "UPDATE fpoc.simpli_visits SET status='failed', "
                        "checkout_observation = ? WHERE id = ?",
                        f"[sim] {motivo}", next_stop_id,
                    )
                else:
                    cur.execute(
                        "UPDATE fpoc.simpli_visits SET status='completed' WHERE id = ?",
                        next_stop_id,
                    )
                # Mover driver al stop
                cur.execute(
                    "UPDATE fpoc.driver_positions "
                    "SET current_stop = ?, next_stop = ?, lat = ?, lon = ?, "
                    "    ts_sim = ?, status = 'en_ruta', speed_kmh = ?, updated_at = SYSDATETIME() "
                    "WHERE vehicle_id = ? AND planned_date = ?",
                    next_order, next_order + 1, target_lat, target_lon,
                    sim_clock, rng.uniform(15, 35),
                    vehicle_id, fecha_iso,
                )
            elif d.lat is not None and d.lon is not None and target_lat and target_lon:
                # Interpolar pos hacia el siguiente stop
                # frac = (sim_clock - last_pos_ts) / (eta - last_pos_ts) — aprox simple
                # Como no guardamos last_pos_ts, usamos un avance fraccional fijo: 0.5 por tick
                frac = 0.5
                new_lat = d.lat + (target_lat - d.lat) * frac
                new_lon = d.lon + (target_lon - d.lon) * frac
                dist_km = _haversine_km(d.lat, d.lon, new_lat, new_lon)
                # Speed = dist / (sim_minutes_per_tick / 60) — aprox
                speed = dist_km / max(0.1, SIM_MINUTES_PER_TICK / 60.0)
                # Random delay: detener al driver con cierta prob
                status = 'entregando' if rng.random() < DELIVERY_DELAY_PROB else 'en_ruta'
                cur.execute(
                    "UPDATE fpoc.driver_positions "
                    "SET lat = ?, lon = ?, ts_sim = ?, status = ?, "
                    "    speed_kmh = ?, updated_at = SYSDATETIME() "
                    "WHERE vehicle_id = ? AND planned_date = ?",
                    new_lat, new_lon, sim_clock, status, speed,
                    vehicle_id, fecha_iso,
                )
            cn.commit()


# ============================================================================
# Endpoints
# ============================================================================
class DriverPosition(BaseModel):
    vehicle_id: int
    ruta_id: Optional[str] = None
    driver_name: Optional[str] = None
    patente: Optional[int] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    current_stop: Optional[int] = None
    next_stop: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    ts_sim: Optional[str] = None
    status: str
    speed_kmh: Optional[float] = None
    stops_total: int = 0
    stops_completed: int = 0
    stops_failed: int = 0
    vip_visitas: int = 0


class SimStatusResponse(BaseModel):
    sim_active: bool
    sim_clock: Optional[str] = None
    tick_sec: int
    minutes_per_tick: int
    drivers: list[DriverPosition]


@router.get("/driver-positions", response_model=SimStatusResponse)
def driver_positions(
    fecha: str = Query(...),
    empresa_id: Optional[int] = Query(None),
    user: CurrentUser = Depends(current_user),
) -> SimStatusResponse:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    scope_where = ""
    scope_params: list = []
    # Para transport_manager, solo ver vehículos de su empresa
    if not user.is_falabella and user.empresa_id is not None:
        scope_where = " AND v.empresa_falsa = ?"
        scope_params.append(user.empresa_id)
    elif empresa_id is not None:
        scope_where = " AND v.empresa_falsa = ?"
        scope_params.append(empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT dp.vehicle_id, dp.ruta_id, dp.driver_name, dp.patente_falsa,
                       dp.current_stop, dp.next_stop, dp.lat, dp.lon,
                       dp.ts_sim, dp.status, dp.speed_kmh,
                       MAX(v.empresa_falsa) AS empresa_id,
                       MAX(et.nombre) AS empresa_nombre,
                       COUNT(v.id) AS stops_total,
                       SUM(CASE WHEN v.status='completed' THEN 1 ELSE 0 END) AS stops_completed,
                       SUM(CASE WHEN v.status='failed' THEN 1 ELSE 0 END) AS stops_failed,
                       SUM(CASE WHEN vc.match_value IS NOT NULL THEN 1 ELSE 0 END) AS vip_visitas
                FROM fpoc.driver_positions dp
                LEFT JOIN fpoc.simpli_visits v
                   ON v.patente_falsa = dp.vehicle_id AND v.planned_date = dp.planned_date
                LEFT JOIN fpoc.empresas_transporte et
                   ON et.empresa_id = v.empresa_falsa
                LEFT JOIN fpoc.vip_clients vc
                   ON vc.active = 1 AND vc.match_type = 'title' AND vc.match_value = v.title
                WHERE dp.planned_date = ?{scope_where}
                GROUP BY dp.vehicle_id, dp.ruta_id, dp.driver_name, dp.patente_falsa,
                         dp.current_stop, dp.next_stop, dp.lat, dp.lon,
                         dp.ts_sim, dp.status, dp.speed_kmh
                HAVING (? IS NULL OR MAX(v.empresa_falsa) = ?)""",
            fecha, *scope_params, empresa_id, empresa_id,
        )
        rows = cur.fetchall()

    sim_clock_str = None
    with _STATE.lock:
        if fecha in _STATE.active_dates:
            sim_clock_str = _STATE.active_dates[fecha].isoformat()

    drivers = [DriverPosition(
        vehicle_id=int(r.vehicle_id),
        ruta_id=r.ruta_id,
        driver_name=r.driver_name,
        patente=int(r.patente_falsa) if r.patente_falsa is not None else None,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        empresa_nombre=r.empresa_nombre,
        current_stop=int(r.current_stop) if r.current_stop is not None else None,
        next_stop=int(r.next_stop) if r.next_stop is not None else None,
        lat=float(r.lat) if r.lat is not None else None,
        lon=float(r.lon) if r.lon is not None else None,
        ts_sim=str(r.ts_sim) if r.ts_sim else None,
        status=str(r.status) if r.status else "en_ruta",
        speed_kmh=float(r.speed_kmh) if r.speed_kmh is not None else None,
        stops_total=int(r.stops_total or 0),
        stops_completed=int(r.stops_completed or 0),
        stops_failed=int(r.stops_failed or 0),
        vip_visitas=int(r.vip_visitas or 0),
    ) for r in rows]

    return SimStatusResponse(
        sim_active=fecha in _STATE.active_dates,
        sim_clock=sim_clock_str,
        tick_sec=SIM_TICK_SEC,
        minutes_per_tick=SIM_MINUTES_PER_TICK,
        drivers=drivers,
    )

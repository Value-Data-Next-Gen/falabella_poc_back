"""Endpoints del piloto controlable (Fase 3 MVP).

Centraliza todo lo necesario para correr una demo / pilot interno de la torre
de control sin depender de datos reales del XLSX SimpliRoute:

- POST   /api/admin/pilot/setup          -> arma un dia con drivers + visitas
                                            distribuidas en clientes hardcodeados.
- GET    /api/admin/pilot/clock          -> sim_clock actual + offset_min.
- POST   /api/admin/pilot/clock          -> avanza/resetea el offset manual.
- POST   /api/admin/pilot/simulate-event -> aplica un evento (delay/complete/no_show).
- GET    /api/admin/pilot/status         -> resumen del piloto del dia.

Auth: solo `falabella_admin` / `falabella_ops` (rol `is_falabella`).

Acompana al router `admin_day_notifications` (notify-day-start, notify-eta-breach):
los endpoints de piloto son las "palancas" que el operador opera en pantalla,
mientras que `eta_breach_cron` corre automatico cada 5 min en background.
"""
from __future__ import annotations

import random
from datetime import date as _date_cls, datetime, time, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import get_conn
from core.state import (
    STATE,
    advance_sim_clock,
    get_sim_clock,
    reset_sim_clock,
)


router = APIRouter(prefix="/api/admin/pilot", tags=["admin-pilot"])


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def _require_admin_or_ops(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_falabella:
        raise HTTPException(403, "Requiere rol falabella_admin o falabella_ops")
    return user


# ---------------------------------------------------------------------------
# Dataset hardcodeado de clientes por region
# ---------------------------------------------------------------------------
# (nombre, address, comuna, lat, lng). Coordenadas reales (validadas en
# mapas) — alcanzan para que el mapa muestre puntos coherentes en la demo.
CLIENTES_DATASET: dict[str, list[tuple[str, str, str, float, float]]] = {
    "RM": [
        ("Diaz Retail",         "Av Apoquindo 4500",   "Las Condes",   -33.4173, -70.6062),
        ("Rojas SPA",           "Manquehue Sur 1200",  "Las Condes",   -33.4040, -70.5810),
        ("Lopez Import",        "Tobalaba 1888",       "Providencia",  -33.4194, -70.5921),
        ("Tech del Sur",        "Andres Bello 2700",   "Providencia",  -33.4150, -70.6112),
        ("Comercial Mapocho",   "Apoquindo 8500",      "Las Condes",   -33.4080, -70.5500),
        ("Servicios Andes",     "Las Condes 2200",     "Las Condes",   -33.4187, -70.5891),
        ("Bodega Sur",          "Macul 3300",          "Macul",        -33.4900, -70.6020),
        ("Minera Atacama",      "Vitacura 3500",       "Vitacura",     -33.3990, -70.6010),
        ("Distribuidora Norte", "Recoleta 1500",       "Recoleta",     -33.4200, -70.6390),
        ("Comercial Sur",       "San Joaquin 4500",    "San Joaquin",  -33.4960, -70.6260),
    ],
    "Valparaiso": [
        ("Logistica Costa",      "Av Brasil 1000",       "Valparaiso",  -33.0458, -71.6197),
        ("Importadora Pacifico", "Pedro Montt 1700",     "Valparaiso",  -33.0470, -71.6160),
        ("Servicios Mar",        "Av Espana 1200",       "Vina del Mar", -33.0250, -71.5520),
        ("Comercial Reina",      "1 Norte 1100",         "Vina del Mar", -33.0220, -71.5510),
        ("Vinaltur",             "Libertad 800",         "Vina del Mar", -33.0260, -71.5540),
        ("Puerto Trading",       "Cochrane 654",         "Valparaiso",  -33.0420, -71.6230),
        ("Achupallas Retail",    "Achupallas 2000",      "Vina del Mar", -33.0080, -71.5180),
    ],
    "Biobio": [
        ("BioBio Comercial",     "Diagonal Pedro Aguirre Cerda 1100", "Concepcion", -36.8270, -73.0500),
        ("Lautaro Distrib",      "OHiggins 800",         "Concepcion",  -36.8240, -73.0460),
        ("Talca Logistica",      "Colon 1500",           "Talcahuano",  -36.7240, -73.1170),
        ("Penco Bodegas",        "Penco 750",            "Talcahuano",  -36.7280, -73.1080),
    ],
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ClockResponse(BaseModel):
    fecha: str
    sim_clock: str          # ISO datetime
    offset_min: int
    mode: Literal["auto", "manual"]


class ClockActionRequest(BaseModel):
    fecha: str = Field(..., description="YYYY-MM-DD")
    action: Literal["advance", "reset"]
    minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=24 * 60,
        description="Requerido si action=advance. Minutos a sumar al offset.",
    )


class PilotSetupRequest(BaseModel):
    fecha: str = Field(..., description="YYYY-MM-DD")
    driver_ids: list[str] = Field(..., min_length=1)
    regiones: list[str] = Field(..., min_length=1)
    visitas_por_driver: int = Field(..., ge=1, le=20)
    horario_inicio: str = Field(default="09:00", description="HH:MM")
    horario_fin: str = Field(default="18:00", description="HH:MM")
    auto_start_day: bool = True


class PilotDriverResult(BaseModel):
    driver_id: str
    driver_name: Optional[str] = None
    vehicle_id: int
    visitas: int


class PilotSetupResponse(BaseModel):
    fecha: str
    created: int
    drivers: list[PilotDriverResult]
    day_state: str
    regiones_used: list[str]


class SimulateEventRequest(BaseModel):
    tracking_id: str = Field(..., min_length=1, max_length=64)
    event: Literal["delay", "complete", "no_show"]


class SimulateEventResponse(BaseModel):
    tracking_id: str
    event: str
    status: str
    current_eta_cl: Optional[str] = None
    detail: Optional[str] = None


class PilotStatusDriverItem(BaseModel):
    driver_id: str
    driver_name: Optional[str] = None
    vehicle_id: int
    pending: int
    completed: int
    failed: int
    total: int


class PilotStatusResponse(BaseModel):
    fecha: str
    sim_clock: str
    offset_min: int
    mode: Literal["auto", "manual"]
    day_state: str
    drivers: list[PilotStatusDriverItem]
    totals: dict
    next_eta_breach_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_fecha(fecha: str) -> _date_cls:
    try:
        return _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha invalida: {fecha!r}")


def _parse_hhmm(hhmm: str) -> time:
    try:
        h, m = hhmm.split(":")
        return time(int(h), int(m))
    except Exception:  # noqa: BLE001
        raise HTTPException(400, f"horario invalido: {hhmm!r} (esperado HH:MM)")


def _resolve_driver(driver_id: str) -> dict:
    """Resuelve driver_id -> dict con name, vehicle_id, empresa_id."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT driver_id, name, vehicle_id, empresa_id "
            "FROM fpoc.drivers WHERE driver_id = ? AND active = 1",
            driver_id,
        )
        r = cur.fetchone()
    if r is None:
        raise HTTPException(404, f"driver {driver_id!r} no encontrado o inactivo")
    if r.vehicle_id is None:
        raise HTTPException(409, f"driver {driver_id!r} sin vehicle_id asignado")
    return {
        "driver_id": str(r.driver_id),
        "name": str(r.name) if r.name else None,
        "vehicle_id": int(r.vehicle_id),
        "empresa_id": int(r.empresa_id) if r.empresa_id is not None else 0,
    }


def _evenly_spaced_etas(
    fecha: _date_cls, t_ini: time, t_fin: time, n: int,
) -> list[datetime]:
    """Distribuye N ETAs uniformemente entre t_ini y t_fin.

    Ej: n=3, 09:00->18:00 => 11:15, 13:30, 15:45 (intervalos iguales).
    Formula: ETA_i = t_ini + (i+1)/(n+1) * (t_fin - t_ini), i = 0..n-1
    """
    base_ini = datetime.combine(fecha, t_ini)
    base_fin = datetime.combine(fecha, t_fin)
    total = (base_fin - base_ini).total_seconds()
    if total <= 0:
        raise HTTPException(400, "horario_fin debe ser posterior a horario_inicio")
    etas: list[datetime] = []
    for i in range(n):
        frac = (i + 1) / (n + 1)
        etas.append(base_ini + timedelta(seconds=total * frac))
    return etas


def _next_visit_id(cn) -> int:
    """Obtiene MAX(id)+1 para insertar una visita nueva en simpli_visits.
    Como id es PK BIGINT no autoincremental, lo asignamos manualmente.
    """
    cur = cn.cursor()
    cur.execute("SELECT ISNULL(MAX(id), 0) AS max_id FROM fpoc.simpli_visits")
    r = cur.fetchone()
    return int(r.max_id or 0) + 1


def _maybe_transition_day_to_en_curso(
    fecha: str, user: CurrentUser,
) -> str:
    """Si auto_start_day, transiciona BORRADOR -> VALIDADO -> EN_CURSO.

    Reusa la logica del router day_state (validar prep + start). Si ya esta
    en EN_CURSO, no hace nada. Devuelve el estado final.
    """
    from routers.day_state import _build_state, _ensure_row
    cur_state = _build_state(fecha, user)
    if cur_state.state == "EN_CURSO":
        return "EN_CURSO"

    # Avanzar progresivamente. Forzamos BORRADOR -> VALIDADO -> EN_CURSO en
    # 2 UPDATEs directos (saltando validaciones de prep_ok). El piloto se
    # presume internamente sano (drivers activos + visitas insertadas recien).
    with get_conn() as cn:
        _ensure_row(cn, fecha, user.user_id)
        cur = cn.cursor()
        from random import randint
        seed = randint(1, 999_999)
        cur.execute(
            "UPDATE fpoc.planificacion_imports "
            "SET state = 'EN_CURSO', "
            "    started_at = SYSDATETIME(), "
            "    started_by_user_id = ?, "
            "    day_seed = ? "
            "WHERE fecha = ?",
            user.user_id, seed, fecha,
        )
        # Cerrar otros dias EN_CURSO (invariante un-solo-dia-abierto).
        cur.execute(
            "UPDATE fpoc.planificacion_imports "
            "SET state = 'CERRADO', closed_at = SYSDATETIME() "
            "WHERE state = 'EN_CURSO' AND fecha <> ?",
            fecha,
        )
        cn.commit()

    # Sync STATE.today
    try:
        STATE.today = _date_cls.fromisoformat(fecha)
    except Exception:  # noqa: BLE001
        pass
    # Invalidar caches del day-state
    try:
        from routers.day_state import _invalidate_state_caches
        _invalidate_state_caches()
    except Exception:  # noqa: BLE001
        pass
    return "EN_CURSO"


def _day_state(fecha: str) -> str:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT state FROM fpoc.planificacion_imports WHERE fecha = ?",
            fecha,
        )
        r = cur.fetchone()
    if r is None or r[0] is None:
        return "BORRADOR"
    return str(r[0])


# ---------------------------------------------------------------------------
# Endpoint: GET/POST clock
# ---------------------------------------------------------------------------

@router.get("/clock", response_model=ClockResponse)
def get_clock(
    fecha: str = Query(..., description="YYYY-MM-DD"),
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> ClockResponse:
    fecha_obj = _validate_fecha(fecha)
    sc = get_sim_clock(fecha_obj)
    # leer offset desde DB directamente para reportarlo
    from core.state import _read_offset, _to_iso_date
    offset = _read_offset(_to_iso_date(fecha_obj))
    return ClockResponse(
        fecha=fecha,
        sim_clock=sc.isoformat(),
        offset_min=offset,
        mode=("manual" if offset != 0 else "auto"),
    )


@router.post("/clock", response_model=ClockResponse)
def post_clock(
    req: ClockActionRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> ClockResponse:
    fecha_obj = _validate_fecha(req.fecha)
    if req.action == "advance":
        if not req.minutes or req.minutes <= 0:
            raise HTTPException(400, "minutes > 0 requerido para action=advance")
        new_offset = advance_sim_clock(fecha_obj, int(req.minutes))
        logger.info(
            f"[pilot-clock] {req.fecha} advance +{req.minutes}min => offset={new_offset}"
        )
    else:  # reset
        reset_sim_clock(fecha_obj)
        new_offset = 0
        logger.info(f"[pilot-clock] {req.fecha} reset => offset=0")
    sc = get_sim_clock(fecha_obj)
    return ClockResponse(
        fecha=req.fecha,
        sim_clock=sc.isoformat(),
        offset_min=new_offset,
        mode=("manual" if new_offset != 0 else "auto"),
    )


# ---------------------------------------------------------------------------
# Endpoint: setup (sembrar dia con visitas hardcodeadas)
# ---------------------------------------------------------------------------

@router.post("/setup", response_model=PilotSetupResponse)
def pilot_setup(
    req: PilotSetupRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> PilotSetupResponse:
    fecha_obj = _validate_fecha(req.fecha)
    t_ini = _parse_hhmm(req.horario_inicio)
    t_fin = _parse_hhmm(req.horario_fin)

    # Validar regiones
    pool: list[tuple[str, str, str, float, float]] = []
    regiones_used: list[str] = []
    for reg in req.regiones:
        ds = CLIENTES_DATASET.get(reg)
        if not ds:
            raise HTTPException(
                400, f"region {reg!r} no soportada. Disponibles: {sorted(CLIENTES_DATASET.keys())}"
            )
        pool.extend(ds)
        regiones_used.append(reg)

    if len(pool) < req.visitas_por_driver * len(req.driver_ids):
        # Permitimos repetidos entre drivers (cada driver muestrea
        # independientemente), pero dentro de un driver no se repite.
        if len(pool) < req.visitas_por_driver:
            raise HTTPException(
                400,
                f"pool de clientes ({len(pool)}) < visitas_por_driver "
                f"({req.visitas_por_driver}) para alguna region. Ampliar regiones.",
            )

    # Resolver drivers
    resolved: list[dict] = [_resolve_driver(did) for did in req.driver_ids]

    # Anti-conflict: 2 drivers no pueden compartir vehicle_id (las visitas
    # son por patente_falsa). Si hay duplicados, error 409 al user con
    # detalle para que pueda corregir la asignación driver↔vehículo.
    vehicle_to_drivers: dict[int, list[str]] = {}
    for d in resolved:
        vehicle_to_drivers.setdefault(d["vehicle_id"], []).append(d["driver_id"])
    dupes = {v: ds for v, ds in vehicle_to_drivers.items() if len(ds) > 1}
    if dupes:
        msg = ", ".join(
            f"vehicle_id={v} compartido por {ds}" for v, ds in dupes.items()
        )
        raise HTTPException(
            409,
            f"Conflict driver↔vehicle: {msg}. Asigná vehículos distintos a "
            f"cada driver antes de armar el piloto (Mantenedores → Drivers).",
        )

    # Limpieza previa: borrar visitas del dia para los vehicle_ids elegidos
    vehicle_ids = [d["vehicle_id"] for d in resolved]
    if vehicle_ids:
        placeholders = ",".join("?" * len(vehicle_ids))
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                f"DELETE FROM fpoc.simpli_visits "
                f"WHERE planned_date = ? AND patente_falsa IN ({placeholders})",
                req.fecha, *vehicle_ids,
            )
            cn.commit()

    # ETAs uniformes
    etas = _evenly_spaced_etas(fecha_obj, t_ini, t_fin, req.visitas_por_driver)

    # Insert
    created_total = 0
    drivers_out: list[PilotDriverResult] = []
    rng = random.Random(int(fecha_obj.toordinal()))

    with get_conn() as cn:
        cur = cn.cursor()
        next_id = _next_visit_id(cn)
        for d in resolved:
            # Muestreo sin repeticion para este driver
            chosen = rng.sample(pool, k=req.visitas_por_driver)
            for order_idx, (eta_dt, cliente) in enumerate(zip(etas, chosen), start=1):
                nombre, address, comuna, lat, lng = cliente
                vid = next_id
                next_id += 1
                # Construir row con todos los NOT NULL de la tabla. Valores
                # default neutros donde no aplica al piloto (campos del XLSX
                # historico que el frontend no usa).
                ruta_id = f"PILOT-{fecha_obj.isoformat()}-{d['vehicle_id']}"
                reference = int(f"99{d['vehicle_id']:04d}{order_idx:02d}")
                row_params = (
                    req.fecha,                   # planned_date
                    vid,                         # id
                    nombre,                      # title
                    order_idx,                   # order
                    address,                     # address
                    eta_dt,                      # checkout_cl (placeholder)
                    eta_dt,                      # current_eta_cl
                    "pending",                   # status
                    None, None,                  # checkout_comment, checkout_observation
                    reference,                   # reference
                    "CL",                        # country
                    0.0, 0.0, 0.0,               # sla_hour..., bin_start, bin_end
                    "00-00",                     # bin_label
                    0,                           # bin_index
                    "CT-PILOT",                  # ct
                    d["vehicle_id"],             # patente_falsa
                    d["empresa_id"],             # empresa_falsa
                    d["name"] or d["driver_id"], # driver_name
                    fecha_obj.isoformat(),       # fecha_inicio_ruta
                    t_ini,                       # fecha_inicio_ruta_hora_cl
                    0, 0,                        # fechas_futuras_bq, finicio_currenteta_bq
                    0, 0,                        # current_eta_cl_fechainicioruta + _dates
                    0, 0,                        # ruta_eta_futuro, ruta_fecha_inicio_mayor_eta
                    0, 0,                        # ruta_primer_punto_lejano, ruta_fecha_inicio_distinta_fecha_eta
                    "AM" if eta_dt.hour < 12 else "PM",  # am_pm
                    0,                           # ruta_anomala
                    # Columnas agregadas en migraciones 026 + 027:
                    "RM" if comuna in ("Las Condes", "Providencia", "Vitacura",
                                       "Macul", "Recoleta", "San Joaquin",
                                       "Nunoa", "Santiago", "Maipu",
                                       "Puente Alto", "La Florida",
                                       "Quilicura", "Independencia") else "regiones",
                    comuna,                      # comuna
                    ruta_id,                     # ruta_id
                    lat,                         # latitude
                    lng,                         # longitude
                )
                cur.execute(
                    """
                    INSERT INTO fpoc.simpli_visits (
                        planned_date, id, title, [order], address,
                        checkout_cl, current_eta_cl, status,
                        checkout_comment, checkout_observation, reference, country,
                        sla_hour_checkout_eta, bin_start, bin_end, bin_label, bin_index,
                        ct, patente_falsa, empresa_falsa, driver_name,
                        fecha_inicio_ruta, fecha_inicio_ruta_hora_cl,
                        fechas_futuras_bq, finicio_currenteta_bq,
                        current_eta_cl_fechainicioruta, current_eta_cl_fechainicioruta_dates,
                        ruta_eta_futuro, ruta_fecha_inicio_mayor_eta,
                        ruta_primer_punto_lejano, ruta_fecha_inicio_distinta_fecha_eta,
                        am_pm, ruta_anomala,
                        region, comuna, ruta_id, latitude, longitude
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    *row_params,
                )
                created_total += 1
            drivers_out.append(PilotDriverResult(
                driver_id=d["driver_id"],
                driver_name=d["name"],
                vehicle_id=d["vehicle_id"],
                visitas=req.visitas_por_driver,
            ))
        cn.commit()

    # Asegurar fila en planificacion_imports + count + transicion opcional.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT 1 FROM fpoc.planificacion_imports WHERE fecha = ?",
            req.fecha,
        )
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute(
                "INSERT INTO fpoc.planificacion_imports "
                "(fecha, count, imported_by_user_id, state) VALUES (?, ?, ?, 'BORRADOR')",
                req.fecha, created_total, user.user_id,
            )
        else:
            cur.execute(
                "UPDATE fpoc.planificacion_imports SET count = "
                "(SELECT COUNT(*) FROM fpoc.simpli_visits WHERE planned_date = ?) "
                "WHERE fecha = ?",
                req.fecha, req.fecha,
            )
        cn.commit()

    final_state = _day_state(req.fecha)
    if req.auto_start_day and final_state != "EN_CURSO":
        try:
            final_state = _maybe_transition_day_to_en_curso(req.fecha, user)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pilot-setup] auto_start_day fallo: {e}")
            final_state = _day_state(req.fecha)

    logger.info(
        f"[pilot-setup] {req.fecha} drivers={len(resolved)} visitas={created_total} "
        f"day_state={final_state}"
    )
    return PilotSetupResponse(
        fecha=req.fecha,
        created=created_total,
        drivers=drivers_out,
        day_state=final_state,
        regiones_used=regiones_used,
    )


# ---------------------------------------------------------------------------
# Endpoint: simulate-event
# ---------------------------------------------------------------------------

@router.post("/simulate-event", response_model=SimulateEventResponse)
def simulate_event(
    req: SimulateEventRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> SimulateEventResponse:
    """Aplica un evento simulado a una visita.

    - delay      => +30min al current_eta_cl + dispara alerta WhatsApp ETA breach.
    - complete   => status='completed'. Sin notificacion (driver lo marco OK).
    - no_show    => status='failed' + comentario alertable 'SIN MORADORES'.
    """
    tid = req.tracking_id.strip()

    # Cargar visita
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT id, status, current_eta_cl, planned_date "
            "FROM fpoc.simpli_visits WHERE CAST(id AS VARCHAR(32)) = ?",
            tid,
        )
        v = cur.fetchone()
    if v is None:
        raise HTTPException(404, f"visita {tid} no existe")

    planned_date = str(v.planned_date)
    sim_clock = get_sim_clock(planned_date)
    detail: Optional[str] = None
    new_status = str(v.status or "pending")
    new_eta: Optional[datetime] = None

    if req.event == "delay":
        new_eta = sim_clock + timedelta(minutes=30)
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.simpli_visits SET current_eta_cl = ? "
                "WHERE CAST(id AS VARCHAR(32)) = ?",
                new_eta, tid,
            )
            cn.commit()
        # Disparar alerta WhatsApp (reusa la logica unificada)
        try:
            from routers.admin_day_notifications import dispatch_eta_breach
            resp = dispatch_eta_breach(tid, triggered_by="pilot_simulate_delay")
            detail = (
                f"ETA +30min => {new_eta.strftime('%H:%M')}. "
                f"Alerta WhatsApp: {resp.status}"
                + (f" ({resp.error})" if resp.error else "")
            )
        except HTTPException as he:
            detail = f"ETA +30min, pero alerta fallo: {he.detail}"
        except Exception as e:  # noqa: BLE001
            detail = f"ETA +30min, pero alerta fallo: {e}"

    elif req.event == "complete":
        # Idempotencia: si la visita ya estaba completed, NO re-disparar notif.
        # Evita duplicados cuando se reseteo el día varias veces en QA o
        # cuando el admin click "complete" dos veces sin querer.
        if str(v.status or "").lower() == "completed":
            return SimulateEventResponse(
                tracking_id=tid,
                event=req.event,
                status="completed",
                current_eta_cl=None,
                detail="Visita ya estaba completed (idempotente, sin notif).",
            )
        # Seteamos checkout_cl = sim_clock (timestamp efectivo de entrega).
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.simpli_visits "
                "SET status = 'completed', checkout_cl = ? "
                "WHERE CAST(id AS VARCHAR(32)) = ?",
                sim_clock, tid,
            )
            cn.commit()
        new_status = "completed"
        try:
            from routers.admin_day_notifications import dispatch_visit_completed
            resp = dispatch_visit_completed(tid, triggered_by="pilot_simulate_complete")
            detail = (
                f"Visita completed. "
                f"Notif: driver={resp.driver_notified} mgrs={resp.manager_notified_count} "
                f"admins={resp.admin_notified_count} ({resp.completed_count}/{resp.total_count})"
            )
        except HTTPException as he:
            detail = f"Visita completed, pero notif fallo: {he.detail}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pilot-simulate-complete] notif fallo TID={tid}: {e}")
            detail = f"Visita completed (notif fallo: {e})."

    elif req.event == "no_show":
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.simpli_visits SET status = 'failed' "
                "WHERE CAST(id AS VARCHAR(32)) = ?",
                tid,
            )
            cn.commit()
        new_status = "failed"
        try:
            from routers.comments import _persist_and_dispatch_comment
            _persist_and_dispatch_comment(
                tracking_id=tid,
                motivo="SIN MORADORES",
                comentario="Simulado via panel piloto",
                user_id=user.user_id,
                user_display_name=user.display_name,
            )
            detail = "Visita marcada failed + comentario SIN MORADORES persistido."
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pilot-simulate] persist comment fallo TID={tid}: {e}")
            detail = f"Visita failed (comment fallo: {e})."

    return SimulateEventResponse(
        tracking_id=tid,
        event=req.event,
        status=new_status,
        current_eta_cl=new_eta.isoformat() if new_eta else None,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Endpoint: status (resumen del piloto)
# ---------------------------------------------------------------------------

@router.get("/status", response_model=PilotStatusResponse)
def pilot_status(
    fecha: str = Query(..., description="YYYY-MM-DD"),
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> PilotStatusResponse:
    fecha_obj = _validate_fecha(fecha)
    sc = get_sim_clock(fecha_obj)
    from core.state import _read_offset, _to_iso_date
    offset = _read_offset(_to_iso_date(fecha_obj))
    day_state = _day_state(fecha)

    # Agrupar por driver
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT d.driver_id, d.name AS driver_name, d.vehicle_id,
                   SUM(CASE WHEN LOWER(v.status) = 'pending'   THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN LOWER(v.status) = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN LOWER(v.status) = 'failed'    THEN 1 ELSE 0 END) AS failed,
                   COUNT(*) AS total
            FROM fpoc.drivers d
            JOIN fpoc.simpli_visits v ON v.patente_falsa = d.vehicle_id
            WHERE v.planned_date = ? AND d.active = 1
            GROUP BY d.driver_id, d.name, d.vehicle_id
            ORDER BY d.driver_id
            """,
            fecha,
        )
        rows = cur.fetchall()

    drivers = [
        PilotStatusDriverItem(
            driver_id=str(r.driver_id),
            driver_name=str(r.driver_name) if r.driver_name else None,
            vehicle_id=int(r.vehicle_id),
            pending=int(r.pending or 0),
            completed=int(r.completed or 0),
            failed=int(r.failed or 0),
            total=int(r.total or 0),
        )
        for r in rows
    ]
    totals = {
        "pending": sum(d.pending for d in drivers),
        "completed": sum(d.completed for d in drivers),
        "failed": sum(d.failed for d in drivers),
        "total": sum(d.total for d in drivers),
        "drivers": len(drivers),
    }

    # Proxima alerta ETA: minima ETA entre pending que ya este vencida o por vencer.
    next_eta_breach: Optional[str] = None
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT MIN(current_eta_cl) AS min_eta "
                "FROM fpoc.simpli_visits "
                "WHERE planned_date = ? AND status = 'pending'",
                fecha,
            )
            r = cur.fetchone()
        if r and r.min_eta:
            next_eta_breach = (
                r.min_eta.isoformat() if hasattr(r.min_eta, "isoformat")
                else str(r.min_eta)
            )
    except Exception:  # noqa: BLE001
        pass

    return PilotStatusResponse(
        fecha=fecha,
        sim_clock=sc.isoformat(),
        offset_min=offset,
        mode=("manual" if offset != 0 else "auto"),
        day_state=day_state,
        drivers=drivers,
        totals=totals,
        next_eta_breach_at=next_eta_breach,
    )

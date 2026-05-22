"""Endpoints operacionales del mapa de la torre de control (Fase 3 MVP).

Reemplaza al viejo `STATE.snapshot_df + driver_sim`. Calcula la posicion
estimada de cada driver para una fecha dada, interpolando linealmente entre
el ultimo stop completado y el proximo pendiente, en funcion del sim_clock
del dia (puede estar en modo automatico o manual via `/api/admin/pilot/clock`).

  GET /api/operacion/driver-positions?fecha=YYYY-MM-DD
"""
from __future__ import annotations

from datetime import date as _date_cls, datetime
from itertools import groupby
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn
from core.state import get_sim_clock


router = APIRouter(prefix="/api/operacion", tags=["operacion"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DriverPosition(BaseModel):
    driver_id: str
    driver_name: Optional[str] = None
    vehicle_id: int
    lat: Optional[float] = None
    lng: Optional[float] = None
    status: str  # 'en_ruta' | 'detenido' | 'finalizado' | 'sin_inicio'
    next_visit_id: Optional[str] = None
    next_visit_title: Optional[str] = None
    next_visit_eta: Optional[str] = None
    last_completed_id: Optional[str] = None
    completed_count: int = 0
    pending_count: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_eta(raw) -> Optional[datetime]:
    """Convierte ETA a datetime. Acepta pyodbc datetime o string."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        s = str(raw).replace("T", " ")
        # 'YYYY-MM-DD HH:MM:SS' o 'YYYY-MM-DD HH:MM'
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                continue
        return None
    except Exception:  # noqa: BLE001
        return None


def _interpolate_position(
    last_done: Optional[dict],
    next_pending: Optional[dict],
    sim_clock: datetime,
) -> tuple[Optional[float], Optional[float], str]:
    """Devuelve (lat, lng, status_label) para la posicion del driver.

    Logica:
      - sin completed y sin pending  -> sin_inicio (None, None)
      - sin completed pero pending   -> en el origen del proximo stop (detenido)
      - completed pero sin pending   -> finalizado en ultimo stop
      - ambos                        -> interpolacion lineal entre A y B segun
                                        sim_clock vs ETAs de A y B.
    """
    # finalizado
    if next_pending is None and last_done is not None:
        return (
            last_done.get("latitude"),
            last_done.get("longitude"),
            "finalizado",
        )
    # sin_inicio
    if next_pending is None and last_done is None:
        return (None, None, "sin_inicio")
    # sin completed: el driver esta esperando salir o ya casi en B
    if last_done is None and next_pending is not None:
        return (
            next_pending.get("latitude"),
            next_pending.get("longitude"),
            "detenido",
        )

    # ambos -> interpolar.
    # Para el completed preferimos checkout_cl (timestamp real de entrega
    # registrado por simulate-event/complete); fallback a current_eta_cl
    # (ETA prevista) si no hay checkout aún.
    eta_a = None
    if last_done:
        eta_a = _parse_eta(last_done.get("checkout_cl")) or _parse_eta(last_done.get("current_eta_cl"))
    eta_b = _parse_eta(next_pending.get("current_eta_cl")) if next_pending else None
    lat_a = last_done.get("latitude") if last_done else None
    lon_a = last_done.get("longitude") if last_done else None
    lat_b = next_pending.get("latitude") if next_pending else None
    lon_b = next_pending.get("longitude") if next_pending else None

    if eta_a is None or eta_b is None or eta_a >= eta_b:
        # Falta data temporal => devolvemos punto medio si tenemos ambos coords
        if all(x is not None for x in (lat_a, lon_a, lat_b, lon_b)):
            return ((lat_a + lat_b) / 2, (lon_a + lon_b) / 2, "en_ruta")
        return (lat_b, lon_b, "en_ruta")

    total = (eta_b - eta_a).total_seconds()
    elapsed = (sim_clock - eta_a).total_seconds()
    if elapsed <= 0:
        # sim_clock todavia anterior al ultimo completado => quedamos en A
        return (lat_a, lon_a, "detenido")
    if elapsed >= total:
        # sim_clock paso B => quedamos en B (deberia haber pasado a completed
        # pero seguimos pintando alli)
        return (lat_b, lon_b, "detenido")

    frac = elapsed / total
    if not all(x is not None for x in (lat_a, lon_a, lat_b, lon_b)):
        return (lat_b, lon_b, "en_ruta")
    return (
        lat_a + frac * (lat_b - lat_a),
        lon_a + frac * (lon_b - lon_a),
        "en_ruta",
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/driver-positions", response_model=list[DriverPosition])
def driver_positions(
    fecha: str = Query(..., description="YYYY-MM-DD"),
    user: CurrentUser = Depends(current_user),
) -> list[DriverPosition]:
    try:
        fecha_obj = _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha invalida: {fecha!r}")
    sim_clock = get_sim_clock(fecha_obj)

    with get_conn() as cn:
        cur = cn.cursor()
        # Multi-tenancy: transport_manager ve solo su empresa.
        params: list = [fecha]
        empresa_filter = ""
        if not user.is_falabella and user.empresa_id is not None:
            empresa_filter = " AND d.empresa_id = ?"
            params.append(user.empresa_id)
        cur.execute(
            f"""
            SELECT d.driver_id, d.name AS driver_name, d.vehicle_id,
                   v.id, v.title, v.status, v.current_eta_cl, v.checkout_cl,
                   v.latitude, v.longitude, v.comuna, v.[order]
            FROM fpoc.drivers d
            JOIN fpoc.simpli_visits v ON v.patente_falsa = d.vehicle_id
            WHERE v.planned_date = ? AND d.active = 1{empresa_filter}
            ORDER BY d.driver_id, v.current_eta_cl, v.[order]
            """,
            *params,
        )
        rows = cur.fetchall()

    # Materializar a dicts (los pyodbc.Row se invalidan al cerrar la conexion).
    materialized = [
        {
            "driver_id": str(r.driver_id),
            "driver_name": r.driver_name,
            "vehicle_id": int(r.vehicle_id),
            "id": str(r.id),
            "title": r.title,
            "status": str(r.status or "").lower(),
            "current_eta_cl": r.current_eta_cl,
            "checkout_cl": r.checkout_cl,
            "latitude": float(r.latitude) if r.latitude is not None else None,
            "longitude": float(r.longitude) if r.longitude is not None else None,
            "comuna": r.comuna,
            "order": int(r.order) if r.order is not None else 0,
        }
        for r in rows
    ]

    positions: list[DriverPosition] = []
    for driver_id, group in groupby(materialized, key=lambda r: r["driver_id"]):
        visits = list(group)
        completed = [v for v in visits if v["status"] == "completed"]
        pending = [v for v in visits if v["status"] == "pending"]
        last_done = completed[-1] if completed else None
        next_one = pending[0] if pending else None

        lat, lng, status_label = _interpolate_position(last_done, next_one, sim_clock)

        next_eta_str: Optional[str] = None
        if next_one and next_one.get("current_eta_cl"):
            eta = next_one["current_eta_cl"]
            next_eta_str = eta.isoformat() if hasattr(eta, "isoformat") else str(eta)

        positions.append(DriverPosition(
            driver_id=driver_id,
            driver_name=visits[0]["driver_name"],
            vehicle_id=visits[0]["vehicle_id"],
            lat=lat,
            lng=lng,
            status=status_label,
            next_visit_id=next_one["id"] if next_one else None,
            next_visit_title=next_one["title"] if next_one else None,
            next_visit_eta=next_eta_str,
            last_completed_id=last_done["id"] if last_done else None,
            completed_count=len(completed),
            pending_count=len(pending),
        ))

    logger.debug(
        f"[driver-positions] fecha={fecha} sim_clock={sim_clock.isoformat()} "
        f"drivers={len(positions)}"
    )
    return positions

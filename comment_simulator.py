"""Simulador de comentarios alertables del transportista.

Para demos: cada N segundos elige una visita pending random y un motivo random
(con peso fuerte hacia los alertables) y dispara `_persist_and_dispatch_comment`.
Eso emite el evento `comment_alert` y, si `ENABLE_AUTO_NOTIFY=true`, manda WhatsApp.

Endpoints (prefijo /api/comment-sim, todos requieren admin salvo `stats`):
    GET  /stats
    POST /toggle           {enabled}
    POST /config           {interval_sec?, only_alertable?, severity_filter?}
    POST /emit-now
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user, require_admin
from comments import (
    DEFAULT_ALERT_CONFIG,
    MOTIVOS_CATALOGO,
    _persist_and_dispatch_comment,
    _resolve_alert_config,
)
from state import STATE


router = APIRouter(prefix="/api/comment-sim", tags=["comment-sim"])

JOB_ID = "comment-sim-tick"


# Catálogo de comentarios random por motivo (lo que escribiría un transportista)
SAMPLE_COMMENTS: dict[str, list[str]] = {
    "SIN MORADORES": [
        "Toqué el timbre 3 veces, no atiende nadie",
        "Llamé al teléfono y suena buzón",
        "Casa con luces apagadas, sin moradores",
    ],
    "PROBLEMA DE DIRECCION/ SIN INFORMACION": [
        "Dirección errónea, no corresponde",
        "Calle no existe en el GPS",
        "Sin numeración, vecinos no conocen al destinatario",
    ],
    "NO DESPACHA A LOCALIDAD": [
        "Fuera de mi zona de despacho",
        "Comuna no atendida por la empresa",
    ],
    "FUERA DE COBERTURA/ FRECUENCIA": [
        "No alcanzo a llegar hoy, fuera de ruta",
        "Frecuencia no cubre esta zona el día de hoy",
    ],
    "PROD N ENTREGADO X TIEMPO": [
        "Se acabó la jornada, no alcancé a entregar",
        "Atrasado por tráfico, se cumplió horario",
    ],
    "PRODUCTO NO CARGADO": [
        "El producto quedó en bodega, no fue cargado",
        "No subió al camión por falta de capacidad",
        "Olvidado en origen, no salió en la ruta",
    ],
    "CLIENTE RECHAZA ENVIO": [
        "El cliente no quiere recibir, anuló compra",
        "Cliente devuelve el paquete sin abrir",
    ],
    "SINIESTRO EN CALLE": [
        "Choque en Av. Apoquindo, esperando carabineros",
        "Encerrona en ruta, intervención policial en curso",
        "Panne mecánica grave, vehículo no puede continuar",
        "Asalto a vehículo, robo de carga parcial",
    ],
    "PRODUCTO CON PROBLEMAS": [
        "Producto llegó roto en la caja",
        "Embalaje deteriorado, falta producto",
        "Caja abierta, faltan unidades",
    ],
    "NO CUMPLE CONDICIONES RETIRO": [
        "Destinatario sin RUT, no acredita identidad",
        "Falta autorización del titular",
    ],
    "PRODUCTO ROBADO": [
        "Carga sustraída del camión durante parada",
        "Robo total del producto en zona insegura",
    ],
}


@dataclass
class SimState:
    enabled: bool = False
    interval_sec: int = 15
    only_alertable: bool = True
    severity_filter: Optional[str] = None  # 'critical' | 'high' | None = todas
    total_emitted_session: int = 0
    last_emit_at: Optional[str] = None
    last_emit_payload: Optional[dict] = None
    last_error: Optional[str] = None
    scheduler: Optional[BackgroundScheduler] = None
    lock: threading.Lock = None  # type: ignore

    def __post_init__(self):
        self.lock = threading.Lock()


SIM = SimState()


# ---------------------------------------------------------------------------
# Selección de motivo + visita
# ---------------------------------------------------------------------------
def _pick_motivo(only_alertable: bool, severity_filter: Optional[str]) -> Optional[str]:
    """Elige un motivo respetando filtros. Considera la config efectiva (no
    sólo defaults) usando empresa_id=None (config global)."""
    candidates: list[str] = []
    weights: list[float] = []
    for m in MOTIVOS_CATALOGO:
        alertable, severity = _resolve_alert_config(m, None)
        if only_alertable and not alertable:
            continue
        if severity_filter and severity != severity_filter:
            continue
        # Más peso a los críticos para que la demo sea visible
        w = {"critical": 3.0, "high": 2.0, "medium": 1.5, "low": 1.0}.get(severity, 1.0)
        candidates.append(m)
        weights.append(w)
    if not candidates:
        return None
    return random.choices(candidates, weights=weights, k=1)[0]


def _pick_tracking_id() -> Optional[tuple[str, str]]:
    """Elige un tracking_id pending random del snapshot. Devuelve (tid, vehicle_name)."""
    if STATE.snapshot_df is None:
        return None
    df = STATE.snapshot_df
    pending = df[df["status"] == "pending"]
    if pending.empty:
        return None
    row = pending.sample(n=1).iloc[0]
    return str(row["tracking_id"]), str(row["vehicle_name"])


def _emit_one() -> Optional[dict]:
    """Genera y persiste UN comentario simulado. Devuelve el payload o None."""
    motivo = _pick_motivo(SIM.only_alertable, SIM.severity_filter)
    if motivo is None:
        SIM.last_error = "Sin motivos que cumplan los filtros"
        return None

    pick = _pick_tracking_id()
    if pick is None:
        SIM.last_error = "No hay visitas pending"
        return None
    tracking_id, vehicle_name = pick

    comentario = random.choice(SAMPLE_COMMENTS.get(motivo, ["(simulado)"]))

    try:
        result = _persist_and_dispatch_comment(
            tracking_id=tracking_id,
            motivo=motivo,
            comentario=comentario,
            user_id=None,
            user_display_name="Simulador (transportista)",
        )
    except Exception as e:  # noqa: BLE001
        SIM.last_error = f"emit falló: {e}"
        logger.warning(f"[comment-sim] {SIM.last_error}")
        return None

    payload = {
        "tracking_id": tracking_id,
        "vehicle_name": vehicle_name,
        "motivo": motivo,
        "comentario": comentario,
        "severity": result.severity,
        "alertable": result.alertable,
    }
    with SIM.lock:
        SIM.total_emitted_session += 1
        SIM.last_emit_at = datetime.utcnow().isoformat()
        SIM.last_emit_payload = payload
        SIM.last_error = None
    return payload


def _tick():
    if not SIM.enabled:
        return
    _emit_one()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def start_scheduler() -> None:
    if SIM.scheduler is not None:
        return
    sched = BackgroundScheduler()
    sched.add_job(
        _tick, "interval", seconds=SIM.interval_sec, id=JOB_ID,
        max_instances=1, coalesce=True,
    )
    sched.start()
    SIM.scheduler = sched
    logger.info(f"[comment-sim] scheduler started (interval={SIM.interval_sec}s enabled={SIM.enabled})")


def stop_scheduler() -> None:
    if SIM.scheduler is not None:
        SIM.scheduler.shutdown(wait=False)
        SIM.scheduler = None


def _reschedule(new_interval: int) -> None:
    if SIM.scheduler is None:
        return
    SIM.scheduler.reschedule_job(JOB_ID, trigger="interval", seconds=new_interval)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SimStats(BaseModel):
    enabled: bool
    interval_sec: int
    only_alertable: bool
    severity_filter: Optional[str] = None
    total_emitted_session: int
    last_emit_at: Optional[str] = None
    last_emit_payload: Optional[dict] = None
    last_error: Optional[str] = None


class SimToggle(BaseModel):
    enabled: bool


class SimConfig(BaseModel):
    interval_sec: Optional[int] = Field(default=None, ge=3, le=600)
    only_alertable: Optional[bool] = None
    severity_filter: Optional[str] = Field(
        default=None, pattern="^(low|medium|high|critical)$",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def _stats_payload() -> SimStats:
    return SimStats(
        enabled=SIM.enabled,
        interval_sec=SIM.interval_sec,
        only_alertable=SIM.only_alertable,
        severity_filter=SIM.severity_filter,
        total_emitted_session=SIM.total_emitted_session,
        last_emit_at=SIM.last_emit_at,
        last_emit_payload=SIM.last_emit_payload,
        last_error=SIM.last_error,
    )


@router.get("/stats", response_model=SimStats)
def get_stats(_: CurrentUser = Depends(current_user)) -> SimStats:
    return _stats_payload()


@router.post("/toggle", response_model=SimStats)
def toggle(req: SimToggle, _: CurrentUser = Depends(require_admin)) -> SimStats:
    SIM.enabled = bool(req.enabled)
    logger.info(f"[comment-sim] enabled={SIM.enabled}")
    return _stats_payload()


@router.post("/config", response_model=SimStats)
def update_config(req: SimConfig, _: CurrentUser = Depends(require_admin)) -> SimStats:
    if req.interval_sec is not None and req.interval_sec != SIM.interval_sec:
        SIM.interval_sec = req.interval_sec
        _reschedule(req.interval_sec)
    if req.only_alertable is not None:
        SIM.only_alertable = req.only_alertable
    if req.severity_filter is not None:
        SIM.severity_filter = req.severity_filter
    return _stats_payload()


@router.post("/emit-now", response_model=SimStats)
def emit_now(_: CurrentUser = Depends(require_admin)) -> SimStats:
    payload = _emit_one()
    if payload is None:
        raise HTTPException(409, SIM.last_error or "no se pudo emitir")
    return _stats_payload()

"""Máquina de estados del día operativo.

Estados:
  BORRADOR  — día con visitas cargadas pero sin validar ni iniciar.
              live_gen NO inyecta. reloj NO avanza.
  LISTO     — sin issues bloqueantes; listo para iniciar.
              live_gen NO inyecta todavía.
  EN_CURSO  — el usuario apretó "Iniciar día". live_gen inyecta visitas
              para esta fecha. STATE.today queda fijado a esta fecha.
  PAUSADO   — pausa temporal de la inyección. El día sigue activo pero
              live_gen no inyecta.
  CERRADO   — terminal. Solo lectura.

Transiciones válidas:
  BORRADOR → LISTO    (validate, requiere prep_ok)
  LISTO → EN_CURSO    (start, requiere prep_ok, registra started_at + day_seed)
  EN_CURSO → PAUSADO  (pause)
  PAUSADO → EN_CURSO  (resume)
  EN_CURSO → CERRADO  (close)
  PAUSADO → CERRADO   (close)
  cualquier → BORRADOR (reset; solo admin con confirmación, opcional)

Cualquier transición no listada → 400.
"""
from __future__ import annotations

import os
from datetime import date as _date_cls
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from auth import CurrentUser, current_user, require_admin
from db import get_conn


router = APIRouter(tags=["day-state"])


VALID_STATES = ("BORRADOR", "LISTO", "EN_CURSO", "PAUSADO", "CERRADO")
VALID_TRANSITIONS = {
    "BORRADOR": {"LISTO"},
    "LISTO":    {"BORRADOR", "EN_CURSO"},
    "EN_CURSO": {"PAUSADO", "CERRADO"},
    "PAUSADO":  {"EN_CURSO", "CERRADO"},
    "CERRADO":  set(),  # terminal
}


# ============================================================================
# Schemas
# ============================================================================
class DayState(BaseModel):
    fecha: str
    state: str  # BORRADOR | LISTO | EN_CURSO | PAUSADO | CERRADO
    visitas: int = 0
    imported_at: Optional[str] = None
    imported_by_user_id: Optional[int] = None
    started_at: Optional[str] = None
    started_by_user_id: Optional[int] = None
    started_by_name: Optional[str] = None
    paused_at: Optional[str] = None
    closed_at: Optional[str] = None
    day_seed: Optional[int] = None
    # Validez para LISTO
    prep_ok: bool = False
    conflicts_count: int = 0
    config_issues_count: int = 0
    driver_issues_count: int = 0
    # Mensajes UX
    can_start: bool = False
    can_pause: bool = False
    can_resume: bool = False
    can_close: bool = False
    can_validate: bool = False
    blocked_reason: Optional[str] = None


class TransitionRequest(BaseModel):
    fecha: str
    target: str            # estado destino
    confirm: bool = False  # para start/close/reset
    allow_non_blocking: bool = False  # para start con warnings no bloqueantes


# ============================================================================
# Helpers
# ============================================================================
def _load_day_row(cn, fecha: str):
    cur = cn.cursor()
    cur.execute(
        "SELECT pi.fecha, pi.count, pi.imported_at, pi.imported_by_user_id, "
        "pi.started_at, pi.started_by_user_id, pi.paused_at, pi.closed_at, "
        "pi.day_seed, pi.state, u.display_name AS started_by_name "
        "FROM fpoc.planificacion_imports pi "
        "LEFT JOIN fpoc.users u ON u.user_id = pi.started_by_user_id "
        "WHERE pi.fecha = ?",
        fecha,
    )
    return cur.fetchone()


def _count_visitas(cn, fecha: str) -> int:
    cur = cn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM fpoc.simpli_visits WHERE planned_date = ?",
        fecha,
    )
    r = cur.fetchone()
    return int(r.n or 0)


def _prep_summary(fecha: str, user: CurrentUser) -> dict:
    """Reusa la lógica de drivers_whatsapp._compute_day_prep + dotacion_check."""
    from drivers_whatsapp import _compute_day_prep, _check_dotacion_conflicts
    conflicts = _check_dotacion_conflicts(fecha) if user.is_falabella else []
    prep = _compute_day_prep(fecha, user)
    return {
        "conflicts_count": len(conflicts),
        "config_issues_count": len(prep["config_issues"]),
        "driver_issues_count": len(prep["driver_issues"]),
    }


def _build_state(fecha: str, user: CurrentUser) -> DayState:
    with get_conn() as cn:
        row = _load_day_row(cn, fecha)
        visitas = _count_visitas(cn, fecha)

    state = "BORRADOR"
    imported_at = None
    imported_by = None
    started_at = None
    started_by = None
    started_by_name = None
    paused_at = None
    closed_at = None
    day_seed = None
    if row is not None:
        state = str(row.state) if row.state else "BORRADOR"
        imported_at = str(row.imported_at) if row.imported_at else None
        imported_by = int(row.imported_by_user_id) if row.imported_by_user_id is not None else None
        started_at = str(row.started_at) if row.started_at else None
        started_by = int(row.started_by_user_id) if row.started_by_user_id is not None else None
        started_by_name = str(row.started_by_name) if row.started_by_name else None
        paused_at = str(row.paused_at) if row.paused_at else None
        closed_at = str(row.closed_at) if row.closed_at else None
        day_seed = int(row.day_seed) if row.day_seed is not None else None

    # Si no hay fila pero hay visitas, esto pasa cuando legacy data quedó sin
    # registro en planificacion_imports. Aún así reportamos BORRADOR.
    summary = _prep_summary(fecha, user) if visitas > 0 else {
        "conflicts_count": 0, "config_issues_count": 0, "driver_issues_count": 0,
    }
    prep_ok = (
        visitas > 0
        and summary["conflicts_count"] == 0
        and summary["driver_issues_count"] == 0
    )

    # Auto-promoción BORRADOR → LISTO si prep_ok (no persistido salvo que se llame validate).
    # Reportamos el state real persistido; el frontend decide si llamar a /transition.
    can_validate = (state == "BORRADOR" and prep_ok)
    can_start = (state == "LISTO" and prep_ok)
    can_pause = (state == "EN_CURSO")
    can_resume = (state == "PAUSADO")
    can_close = (state in ("EN_CURSO", "PAUSADO"))

    blocked_reason: Optional[str] = None
    if not prep_ok and state in ("BORRADOR", "LISTO"):
        bits = []
        if visitas == 0:
            bits.append("sin visitas cargadas")
        if summary["conflicts_count"]:
            bits.append(f"{summary['conflicts_count']} conflictos de dotación")
        if summary["driver_issues_count"]:
            bits.append(f"{summary['driver_issues_count']} drivers con problemas")
        blocked_reason = ", ".join(bits) if bits else None

    return DayState(
        fecha=fecha,
        state=state,
        visitas=visitas,
        imported_at=imported_at,
        imported_by_user_id=imported_by,
        started_at=started_at,
        started_by_user_id=started_by,
        started_by_name=started_by_name,
        paused_at=paused_at,
        closed_at=closed_at,
        day_seed=day_seed,
        prep_ok=prep_ok,
        conflicts_count=summary["conflicts_count"],
        config_issues_count=summary["config_issues_count"],
        driver_issues_count=summary["driver_issues_count"],
        can_start=can_start,
        can_pause=can_pause,
        can_resume=can_resume,
        can_close=can_close,
        can_validate=can_validate,
        blocked_reason=blocked_reason,
    )


def _ensure_row(cn, fecha: str, user_id: int) -> None:
    cur = cn.cursor()
    cur.execute("SELECT 1 FROM fpoc.planificacion_imports WHERE fecha = ?", fecha)
    if cur.fetchone():
        return
    cur.execute(
        "INSERT INTO fpoc.planificacion_imports "
        "(fecha, count, imported_by_user_id, state) "
        "VALUES (?, ?, ?, 'BORRADOR')",
        fecha, 0, user_id,
    )
    cn.commit()


# ============================================================================
# Endpoints
# ============================================================================
@router.get("/api/planificacion/day-state", response_model=DayState)
def get_day_state(
    fecha: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> DayState:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    return _build_state(fecha, user)


@router.post("/api/planificacion/day-state/transition", response_model=DayState)
def transition_day_state(
    req: TransitionRequest,
    user: CurrentUser = Depends(current_user),
) -> DayState:
    try:
        _date_cls.fromisoformat(req.fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {req.fecha}")
    target = req.target.upper().strip()
    if target not in VALID_STATES:
        raise HTTPException(400, f"target inválido: {target}")

    current = _build_state(req.fecha, user)
    if target not in VALID_TRANSITIONS.get(current.state, set()):
        raise HTTPException(
            400,
            f"transición inválida: {current.state} → {target}",
        )

    # Reglas específicas
    if target == "LISTO":
        if not current.prep_ok and not req.allow_non_blocking:
            raise HTTPException(
                409,
                f"No se puede pasar a LISTO: {current.blocked_reason or 'issues bloqueantes'}",
            )
    if target == "EN_CURSO":
        # Requiere LISTO previo (ya validado por VALID_TRANSITIONS)
        if not current.prep_ok and not req.allow_non_blocking:
            raise HTTPException(
                409,
                f"No se puede iniciar: {current.blocked_reason}",
            )
        if not req.confirm:
            raise HTTPException(400, "Se requiere confirm=true para iniciar el día")
    if target == "CERRADO" and not req.confirm:
        raise HTTPException(400, "Se requiere confirm=true para cerrar el día")

    # Aplicar transición
    sets: list[str] = ["state = ?"]
    params: list = [target]
    if target == "EN_CURSO" and current.state == "LISTO":
        # Primera vez que se inicia: registrar started_at + day_seed + user
        from random import randint
        seed = randint(1, 999_999)
        sets.append("started_at = SYSDATETIME()")
        sets.append("started_by_user_id = ?")
        params.append(user.user_id)
        sets.append("day_seed = ?")
        params.append(seed)
    if target == "EN_CURSO" and current.state == "PAUSADO":
        # Reanudación: limpiar paused_at
        sets.append("paused_at = NULL")
    if target == "PAUSADO":
        sets.append("paused_at = SYSDATETIME()")
    if target == "CERRADO":
        sets.append("closed_at = SYSDATETIME()")

    with get_conn() as cn:
        # Asegurar que exista la fila
        _ensure_row(cn, req.fecha, user.user_id)
        cur = cn.cursor()
        params.append(req.fecha)
        cur.execute(
            f"UPDATE fpoc.planificacion_imports SET {', '.join(sets)} WHERE fecha = ?",
            *params,
        )
        cn.commit()

    # Lado del live_generator: arrancar/pausar según target
    try:
        from live_generator import STATE as LIVE_STATE
        if target == "EN_CURSO":
            LIVE_STATE.enabled = True
            # Set STATE.today para que el ML snapshot también apunte ahí
            try:
                from state import STATE
                STATE.reset_day(start_date=_date_cls.fromisoformat(req.fecha))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[day-state] STATE.reset_day falló: {e}")
        elif target in ("PAUSADO", "CERRADO"):
            LIVE_STATE.enabled = False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[day-state] live_gen control falló: {e}")

    logger.info(
        f"[day-state] {req.fecha}: {current.state} → {target} by user_id={user.user_id}"
    )
    return _build_state(req.fecha, user)


@router.post("/api/planificacion/day-state/reset", response_model=DayState)
def reset_day_state(
    fecha: str = Query(...),
    user: CurrentUser = Depends(require_admin),
) -> DayState:
    """Reset destructivo: vuelve a BORRADOR y limpia started/paused/closed.

    Solo admin + flag DEMO_QA=true en env (para no usarse en prod por accidente).
    """
    if os.environ.get("DEMO_QA", "").lower() != "true":
        raise HTTPException(403, "Reset solo disponible con DEMO_QA=true en env")
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE fpoc.planificacion_imports "
            "SET state = 'BORRADOR', started_at = NULL, started_by_user_id = NULL, "
            "    paused_at = NULL, closed_at = NULL, day_seed = NULL "
            "WHERE fecha = ?",
            fecha,
        )
        cn.commit()
    try:
        from live_generator import STATE as LIVE_STATE
        LIVE_STATE.enabled = False
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[day-state] {fecha}: RESET → BORRADOR by user_id={user.user_id}")
    return _build_state(fecha, user)

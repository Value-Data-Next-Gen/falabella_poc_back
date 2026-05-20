"""Máquina de estados del día operativo (Ronda 3).

Estados:
  BORRADOR  — día con visitas cargadas pero sin validar ni iniciar.
              live_gen NO inyecta. reloj NO avanza.
  VALIDADO  — sin issues bloqueantes; listo para iniciar.
              live_gen NO inyecta todavía.
  EN_CURSO  — el usuario apretó "Iniciar día". live_gen inyecta visitas
              para esta fecha. STATE.today queda fijado a esta fecha.
  CERRADO   — terminal. Solo lectura.

Transiciones válidas:
  BORRADOR → VALIDADO  (validate, requiere prep_ok)
  VALIDADO → BORRADOR  (volver a editar si surge un problema)
  VALIDADO → EN_CURSO  (start, registra started_at + day_seed)
  EN_CURSO → CERRADO   (close)
  cualquier → BORRADOR (reset; solo admin con DEMO_QA=true)

PAUSADO se eliminó en Ronda 3. La pausa operativa ahora es responsabilidad
del live_gen (que tiene su propio toggle vía /api/live-gen/toggle).
Para el modelo del día, EN_CURSO → CERRADO directo.

Cualquier transición no listada → 400.
"""
from __future__ import annotations

import os
from datetime import date as _date_cls, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from core.auth import CurrentUser, current_user, require_admin
from core.cache import ttl_cached, invalidate_prefix
from core.db import get_conn


router = APIRouter(prefix="/api/planificacion/day-state", tags=["day-state"])


VALID_STATES = ("BORRADOR", "VALIDADO", "EN_CURSO", "CERRADO")
VALID_TRANSITIONS = {
    "BORRADOR": {"VALIDADO"},
    "VALIDADO": {"BORRADOR", "EN_CURSO"},
    "EN_CURSO": {"CERRADO"},
    "CERRADO":  set(),  # terminal
}
# Backcompat: aceptar 'LISTO' como alias entrante (frontend viejo). Se mapea a
# VALIDADO antes de validar transición.
_TARGET_ALIAS = {"LISTO": "VALIDADO"}


# ============================================================================
# Schemas
# ============================================================================
class DayState(BaseModel):
    fecha: str
    state: str  # BORRADOR | VALIDADO | EN_CURSO | CERRADO
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
    from routers.drivers_whatsapp import _compute_day_prep, _check_dotacion_conflicts
    conflicts = _check_dotacion_conflicts(fecha) if user.is_falabella else []
    prep = _compute_day_prep(fecha, user)
    return {
        "conflicts_count": len(conflicts),
        "config_issues_count": len(prep["config_issues"]),
        "driver_issues_count": len(prep["driver_issues"]),
    }


@ttl_cached(ttl_seconds=20)
def _build_state_raw(fecha: str) -> tuple:
    """Parte cara de _build_state: queries SQL. Devuelve (row_dict, visitas).
    Cache TTL 20s. CRÍTICO: convertimos `pyodbc.Row` a dict ANTES de cachear
    porque pyodbc.Row mantiene referencia al cursor/conn — una vez que sale
    del `with get_conn()`, leer atributos del Row puede devolver None
    (causa `ResponseValidationError: input=None` en el handler transition)."""
    with get_conn() as cn:
        row = _load_day_row(cn, fecha)
        visitas = _count_visitas(cn, fecha)
        # Materializar a dict mientras la conexión está abierta
        row_dict = None
        if row is not None:
            row_dict = {
                "fecha": row.fecha,
                "count": row.count,
                "imported_at": row.imported_at,
                "imported_by_user_id": row.imported_by_user_id,
                "started_at": row.started_at,
                "started_by_user_id": row.started_by_user_id,
                "paused_at": row.paused_at,
                "closed_at": row.closed_at,
                "day_seed": row.day_seed,
                "state": row.state,
                "started_by_name": row.started_by_name,
            }
    return (row_dict, visitas)


def _build_state(fecha: str, user: CurrentUser) -> DayState:
    row, visitas = _build_state_raw(fecha)

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
        # row es ahora un dict (materializado en _build_state_raw para
        # cachear sin perder la conexión pyodbc).
        state = str(row["state"]) if row.get("state") else "BORRADOR"
        imported_at = str(row["imported_at"]) if row.get("imported_at") else None
        imported_by = int(row["imported_by_user_id"]) if row.get("imported_by_user_id") is not None else None
        started_at = str(row["started_at"]) if row.get("started_at") else None
        started_by = int(row["started_by_user_id"]) if row.get("started_by_user_id") is not None else None
        started_by_name = str(row["started_by_name"]) if row.get("started_by_name") else None
        paused_at = str(row["paused_at"]) if row.get("paused_at") else None
        closed_at = str(row["closed_at"]) if row.get("closed_at") else None
        day_seed = int(row["day_seed"]) if row.get("day_seed") is not None else None

    # Si no hay fila pero hay visitas, esto pasa cuando legacy data quedó sin
    # registro en planificacion_imports. Aún así reportamos BORRADOR.
    summary = _prep_summary(fecha, user) if visitas > 0 else {
        "conflicts_count": 0, "config_issues_count": 0, "driver_issues_count": 0,
    }
    # Bloqueantes (impiden VALIDADO sin override):
    #   - sin visitas cargadas
    #   - conflicts_count > 0 (dotación inválida: drivers ausentes/licencia/etc)
    # Warnings (permiten VALIDADO con allow_non_blocking=true):
    #   - driver_issues_count (sin teléfono, sin licencia administrativa)
    #   - config_issues_count (visitas con campos faltantes)
    hard_blocked = visitas == 0 or summary["conflicts_count"] > 0
    prep_ok = not hard_blocked

    # can_validate=true cuando se puede pasar a LISTO (con o sin warnings).
    # El frontend decide si pedir confirmación cuando hay warnings.
    can_validate = (state == "BORRADOR" and not hard_blocked)
    can_start = (state == "VALIDADO" and not hard_blocked)
    can_pause = False  # PAUSADO eliminado en Ronda 3 (compat solo del field)
    can_resume = False
    can_close = (state == "EN_CURSO")

    blocked_reason: Optional[str] = None
    if hard_blocked and state in ("BORRADOR", "VALIDADO"):
        bits = []
        if visitas == 0:
            bits.append("sin visitas cargadas")
        if summary["conflicts_count"]:
            bits.append(f"{summary['conflicts_count']} conflictos de dotación")
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
@router.get("", response_model=DayState)
def get_day_state(
    fecha: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> DayState:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    return _build_state(fecha, user)


def _invalidate_state_caches() -> None:
    """Llamar después de mutar planificacion_imports o simpli_visits para
    forzar refresh inmediato del próximo GET. Sin esto, frontend ve estado
    viejo hasta 5s post-acción."""
    invalidate_prefix("routers.day_state._build_state_raw")
    invalidate_prefix("routers.plan_diario._build_new_from_real")


@router.post("/transition", response_model=DayState)
def transition_day_state(
    req: TransitionRequest,
    user: CurrentUser = Depends(current_user),
) -> DayState:
    try:
        _date_cls.fromisoformat(req.fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {req.fecha}")
    target = req.target.upper().strip()
    # Backcompat: LISTO (R2) → VALIDADO (R3)
    target = _TARGET_ALIAS.get(target, target)
    if target not in VALID_STATES:
        raise HTTPException(400, f"target inválido: {target}")

    current = _build_state(req.fecha, user)
    if target not in VALID_TRANSITIONS.get(current.state, set()):
        raise HTTPException(
            400,
            f"transición inválida: {current.state} → {target}",
        )

    # Reglas específicas
    if target == "VALIDADO":
        if not current.prep_ok and not req.allow_non_blocking:
            raise HTTPException(
                409,
                f"No se puede pasar a VALIDADO: {current.blocked_reason or 'issues bloqueantes'}",
            )
    if target == "EN_CURSO":
        if not current.prep_ok and not req.allow_non_blocking:
            raise HTTPException(
                409,
                f"No se puede iniciar: {current.blocked_reason}",
            )
        if not req.confirm:
            raise HTTPException(400, "Se requiere confirm=true para iniciar el día")
    if target == "CERRADO" and not req.confirm:
        raise HTTPException(400, "Se requiere confirm=true para cerrar el día")

    # R7: invariante un-solo-día-EN_CURSO. Al transicionar a EN_CURSO, primero
    # buscamos otros días EN_CURSO != req.fecha y los cerramos automáticamente
    # con timestamp 'force_closed'. Sin esto el state singleton (state.today)
    # queda apuntando al último iniciado y la app muestra fuentes desalineadas.
    other_open_dates: list[str] = []
    if target == "EN_CURSO":
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT fecha FROM fpoc.planificacion_imports "
                "WHERE state = 'EN_CURSO' AND fecha <> ?",
                req.fecha,
            )
            other_open_dates = [str(r.fecha if hasattr(r, "fecha") else r[0]) for r in cur.fetchall()]
            if other_open_dates:
                cur.execute(
                    "UPDATE fpoc.planificacion_imports "
                    "SET state = 'CERRADO', closed_at = SYSDATETIME() "
                    "WHERE state = 'EN_CURSO' AND fecha <> ?",
                    req.fecha,
                )
                cn.commit()
                logger.warning(
                    f"[day-state] {req.fecha}: cierre forzado de {len(other_open_dates)} "
                    f"día(s) huérfanos EN_CURSO: {other_open_dates}"
                )

    # Aplicar transición
    sets: list[str] = ["state = ?"]
    params: list = [target]
    if target == "EN_CURSO" and current.state == "VALIDADO":
        # Primera vez que se inicia: registrar started_at + day_seed + user
        from random import randint
        seed = randint(1, 999_999)
        sets.append("started_at = SYSDATETIME()")
        sets.append("started_by_user_id = ?")
        params.append(user.user_id)
        sets.append("day_seed = ?")
        params.append(seed)
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

    # Lado del live_generator + driver_sim + comment_sim: arrancar/parar según target
    try:
        from sims.live_generator import STATE as LIVE_STATE
        if target == "EN_CURSO":
            LIVE_STATE.enabled = True
            # Set STATE.today para que el ML snapshot también apunte ahí
            try:
                from core.state import STATE
                STATE.reset_day(start_date=_date_cls.fromisoformat(req.fecha))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[day-state] STATE.reset_day falló: {e}")
            # Arrancar simulación de drivers para esa fecha
            try:
                from sims.driver_sim import start_sim
                start_sim(req.fecha)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[day-state] driver_sim.start_sim falló: {e}")
            # R7: auto-arrancar el comment_simulator. Sin esto el panel
            # Auditoría IA → Alertas IA queda vacío aunque el día esté
            # EN_CURSO porque nadie genera comentarios alertables.
            try:
                from sims.comment_simulator import SIM as COMMENT_SIM
                COMMENT_SIM.enabled = True
                logger.info(f"[day-state] comment_simulator auto-enabled para {req.fecha}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[day-state] comment_sim auto-enable falló: {e}")
        elif target == "CERRADO":
            LIVE_STATE.enabled = False
            try:
                from sims.driver_sim import stop_sim
                stop_sim(req.fecha)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[day-state] driver_sim.stop_sim falló: {e}")
            try:
                from sims.comment_simulator import SIM as COMMENT_SIM
                COMMENT_SIM.enabled = False
            except Exception:  # noqa: BLE001
                pass
        elif target == "BORRADOR":
            # R7: volver a BORRADOR limpia el ring buffer del stream para que
            # el panel de Alertas en vivo no muestre el residuo del día anterior.
            LIVE_STATE.enabled = False
            try:
                from core.events import EVENTS
                n = EVENTS.reset()
                logger.info(f"[day-state] {req.fecha}: buffer eventos limpiado ({n} evt)")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[day-state] EVENTS.reset falló: {e}")
            try:
                from sims.driver_sim import stop_sim
                stop_sim(req.fecha)
            except Exception:  # noqa: BLE001
                pass
            try:
                from sims.comment_simulator import SIM as COMMENT_SIM
                COMMENT_SIM.enabled = False
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[day-state] live_gen control falló: {e}")

    logger.info(
        f"[day-state] {req.fecha}: {current.state} → {target} by user_id={user.user_id}"
    )
    _invalidate_state_caches(); return _build_state(req.fecha, user)


@router.post("/reset", response_model=DayState)
def reset_day_state(
    fecha: str = Query(...),
    user: CurrentUser = Depends(require_admin),
) -> DayState:
    """Reset destructivo: vuelve a BORRADOR y limpia started/paused/closed.

    Permitido solo a admin. En PROD se puede gatear extra con env
    `DAY_STATE_RESET_DISABLED=true` para bloquear el botón. En sandbox/demo
    (estado por default) el admin puede hacer reset sin flag.
    """
    if os.environ.get("DAY_STATE_RESET_DISABLED", "").lower() == "true":
        raise HTTPException(403, "Reset deshabilitado por DAY_STATE_RESET_DISABLED=true")
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
        from sims.live_generator import STATE as LIVE_STATE
        LIVE_STATE.enabled = False
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.events import EVENTS
        EVENTS.reset()
    except Exception:  # noqa: BLE001
        pass
    # Detener driver_sim si estaba simulando esta fecha
    try:
        from sims.driver_sim import stop_sim
        stop_sim(fecha)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[day-state] {fecha}: RESET → BORRADOR by user_id={user.user_id}")
    _invalidate_state_caches(); return _build_state(fecha, user)


class CleanRegenerateResponse(BaseModel):
    fecha: str
    deleted_visits: int
    inserted_visits: int
    state: str


@router.post("/clean-and-regenerate", response_model=CleanRegenerateResponse)
def clean_and_regenerate(
    fecha: str = Query(...),
    rows: int = Query(default=1800, ge=5, le=5000),
    mode: str = Query(default="default", pattern="^(default|minimal)$"),
    user: CurrentUser = Depends(require_admin),
) -> CleanRegenerateResponse:
    """Borra todas las visitas del día y regenera con el live_generator.

    Modos:
      - "default" (1800 rows, multi-empresa, multi-driver, multi-región)
      - "minimal" (5 visitas, 1 empresa, 1 driver, RM, ETAs cronológicas)
        Ignora `rows`. Para demos limpios y tests unitarios.

    El live_generator respeta región por driver (hash determinístico), evita
    líneas cruzando el país. Visitas nacen `pending` para que el sim las
    procese cronológicamente.
    """
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    # Detener driver_sim para esta fecha (vamos a regenerar todo)
    try:
        from sims.driver_sim import stop_sim
        stop_sim(fecha)
    except Exception:  # noqa: BLE001
        pass

    with get_conn() as cn:
        cur = cn.cursor()
        # Borrar visitas existentes del día
        cur.execute(
            "DELETE FROM fpoc.simpli_visits WHERE planned_date = ?", fecha,
        )
        deleted = cur.rowcount or 0
        # Borrar driver_positions del día (las re-inicializamos al re-arrancar el sim)
        cur.execute(
            "DELETE FROM fpoc.driver_positions WHERE planned_date = ?", fecha,
        )
        cn.commit()

    # Insertar batch via live_generator (que ya respeta región por driver)
    try:
        from sims.live_generator import _insert_batch
        with get_conn() as cn:
            inserted = _insert_batch(cn, _date_cls.fromisoformat(fecha), rows, mode=mode)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[clean-regenerate] _insert_batch falló: {e}")
        inserted = 0

    # Asegurar que el registro en planificacion_imports existe y queda en BORRADOR
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT 1 FROM fpoc.planificacion_imports WHERE fecha = ?", fecha,
        )
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO fpoc.planificacion_imports "
                "(fecha, count, imported_by_user_id, state) VALUES (?, ?, ?, 'BORRADOR')",
                fecha, inserted, user.user_id,
            )
        else:
            cur.execute(
                "UPDATE fpoc.planificacion_imports "
                "SET state = 'BORRADOR', count = ?, started_at = NULL, "
                "    started_by_user_id = NULL, paused_at = NULL, closed_at = NULL, "
                "    day_seed = NULL "
                "WHERE fecha = ?",
                inserted, fecha,
            )
        cn.commit()

    logger.info(
        f"[day-state] {fecha}: CLEAN_AND_REGENERATE deleted={deleted} "
        f"inserted={inserted} by user_id={user.user_id}"
    )
    # Invalidar cache ANTES de releer estado (el sed previo no agarró este
    # patrón porque acá es `state = _build_state(...)` no `return _build_state(...)`).
    _invalidate_state_caches()
    state = _build_state(fecha, user)
    return CleanRegenerateResponse(
        fecha=fecha,
        deleted_visits=int(deleted),
        inserted_visits=int(inserted),
        state=state.state,
    )


@router.post("/regenerate", response_model=DayState)
def regenerate_day(
    fecha: str = Query(...),
    user: CurrentUser = Depends(require_admin),
) -> DayState:
    """Rebobina la simulación de un día EN_CURSO sin destruir el plan.

    Qué hace:
      - sim_clock vuelve a 09:00 del día.
      - Todas las visitas del día pasan a status='pending', limpiando observaciones de sim.
      - driver_positions del día se borra y se re-inicializa.
      - El plan (rutas, drivers, asignaciones) se conserva intacto.

    Para qué sirve: "ver de nuevo cómo simula el día" sin tener que volver a
    BORRADOR → VALIDADO → EN_CURSO. Usable solo en EN_CURSO.
    """
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    current = _build_state(fecha, user)
    if current.state != "EN_CURSO":
        raise HTTPException(
            400,
            f"regenerate solo aplica en EN_CURSO (actual: {current.state}). "
            "Para volver a BORRADOR usá /reset."
        )
    with get_conn() as cn:
        cur = cn.cursor()
        # Marcar todas las visitas como pending de nuevo (sim solo: NO toca
        # checkout_cl ni current_eta_cl reales del XLSX — esos son del plan).
        cur.execute(
            "UPDATE fpoc.simpli_visits "
            "SET status = 'pending', checkout_observation = NULL "
            "WHERE planned_date = ?",
            fecha,
        )
        cn.commit()
    # Re-arrancar driver_sim desde 09:00
    try:
        from sims.driver_sim import stop_sim, start_sim
        stop_sim(fecha)
        start_sim(fecha)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[day-state] regenerate: driver_sim re-init falló: {e}")
    # Resetear ring buffer de eventos para que el stream empiece limpio
    try:
        from core.events import EVENTS
        EVENTS.reset()
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[day-state] {fecha}: REGENERATE (rebobinar a 09:00) by user_id={user.user_id}")
    _invalidate_state_caches(); return _build_state(fecha, user)


# =============================================================================
# R7: extender el día — mueve cutoff_time +N minutos
# =============================================================================
class ExtendDayResponse(BaseModel):
    fecha: str
    previous_cutoff: Optional[str] = None
    new_cutoff: str
    pending_visits: int


@router.post("/extend", response_model=ExtendDayResponse)
def extend_day(
    fecha: str = Query(...),
    minutes: int = Query(60, ge=15, le=240),
    user: CurrentUser = Depends(current_user),
) -> ExtendDayResponse:
    """Extiende el cutoff del día +N minutos (default 60, máx 240).

    Útil cuando llega la hora de cierre y aún quedan visitas pendientes.
    Solo si el día está EN_CURSO. Devuelve el cutoff anterior y el nuevo,
    más el conteo de visitas pendientes.
    """
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    from datetime import time as _t, datetime as _dt
    with get_conn() as cn:
        cur = cn.cursor()
        # Verificar estado EN_CURSO
        cur.execute(
            "SELECT state FROM fpoc.planificacion_imports WHERE fecha = ?", fecha,
        )
        row = cur.fetchone()
        if row is None or str(row.state) != "EN_CURSO":
            raise HTTPException(409, f"día {fecha} no está EN_CURSO")

        # Leer cutoff actual (o default 18:30)
        cur.execute(
            "SELECT cutoff_time FROM fpoc.day_config WHERE fecha = ?", fecha,
        )
        cfg = cur.fetchone()
        if cfg is not None and cfg[0] is not None:
            raw = cfg[0]
            if hasattr(raw, "hour"):
                current_t = _t(raw.hour, raw.minute)
            else:
                parts = str(raw).split(":")
                current_t = _t(int(parts[0]), int(parts[1]))
        else:
            current_t = _t(18, 30)
        previous_str = f"{current_t.hour:02d}:{current_t.minute:02d}"

        # Sumar minutos (sin pasar de 23:59)
        base = _dt.combine(_date_cls.fromisoformat(fecha), current_t)
        new_dt = base + timedelta(minutes=minutes)
        if new_dt.date() != _date_cls.fromisoformat(fecha):
            new_dt = _dt.combine(_date_cls.fromisoformat(fecha), _t(23, 59))
        new_str = f"{new_dt.hour:02d}:{new_dt.minute:02d}:00"

        # UPSERT en day_config
        cur.execute("SELECT 1 FROM fpoc.day_config WHERE fecha = ?", fecha)
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO fpoc.day_config (fecha, cutoff_time) VALUES (?, ?)",
                fecha, new_str,
            )
        else:
            cur.execute(
                "UPDATE fpoc.day_config SET cutoff_time = ? WHERE fecha = ?",
                new_str, fecha,
            )
        cn.commit()

        # Conteo pendientes
        cur.execute(
            "SELECT COUNT(*) AS n FROM fpoc.simpli_visits "
            "WHERE planned_date = ? AND status = 'pending'",
            fecha,
        )
        pending = int(cur.fetchone().n or 0)

    logger.info(
        f"[day-state] {fecha}: cutoff extendido +{minutes}min "
        f"({previous_str} → {new_dt.hour:02d}:{new_dt.minute:02d}) "
        f"pendientes={pending} by user_id={user.user_id}"
    )
    return ExtendDayResponse(
        fecha=fecha,
        previous_cutoff=previous_str,
        new_cutoff=f"{new_dt.hour:02d}:{new_dt.minute:02d}",
        pending_visits=pending,
    )

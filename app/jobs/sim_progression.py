"""Sim auto-progression cron (CR-030).

The sim drivers move in space but visitas never flip to a final state unless
some component completes them. This cron closes that loop:

  every `sim_progression_interval_s` seconds (real time):
    if sim_clock.running:
      for each EN_CURSO dia:
        SELECT visitas WHERE estado='pendiente'
                        AND eta_estimada IS NOT NULL
                        AND eta_estimada <= sim_now - grace_min
            ORDER BY eta_estimada ASC
            LIMIT max_per_tick
        for each visita:
          roll an outcome weighted by VIP / non-VIP success rate
          if delivered: estado='entregado', llegada/completada_at set from sim_now
          else:         estado='no_entregado', motivo picked from weighted catalog
          append audit row (tipo='estado_change', user_id=None)
        commit

Notes:
  * Audit `user_id` is `None`: the sim is the actor.
  * The motivo strings are pulled from `core.motivos_catalogo.MOTIVOS` so the
    rest of the pipeline (LLM corrector, alert filters) sees real catalog
    codes — not synthetic ones.
  * Random uses module-level `random.Random()` so tests can seed predictably.
  * No HTTP surface — registration is via `setup_sim_progression_job` called
    from the FastAPI lifespan.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_visita_evento
from app.core.config import settings
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.sim_clock import SimClock
from app.db.models.visita import Visita
from app.db.session import get_sessionmaker

# ----------------------------------------------------------------------------
# Motivo catalog with weights.
#
# Codes MUST match the canonical strings in `core/motivos_catalogo.MOTIVOS`
# so downstream consumers (LLM corrector, severity routing) treat them as real
# catalog motivos. Weights are heuristic — SIN MORADORES is by far the most
# common real-world non-delivery cause; siniestro/daño are rare.
# ----------------------------------------------------------------------------

_FAIL_MOTIVOS: list[tuple[str, float]] = [
    ("SIN MORADORES", 0.40),
    ("PROBLEMA DE DIRECCION/ SIN INFORMACION", 0.25),
    ("CLIENTE RECHAZA", 0.15),
    ("SINIESTRO EN CALLE", 0.10),
    ("PRODUCTO CON PROBLEMAS", 0.10),
]

_rng = random.Random()


def _pick_motivo(rng: random.Random) -> str:
    """Sample one failure motivo by weight. Stable for a given rng state."""
    codes = [m[0] for m in _FAIL_MOTIVOS]
    weights = [m[1] for m in _FAIL_MOTIVOS]
    return rng.choices(codes, weights=weights, k=1)[0]


async def _sim_now_running(db: AsyncSession) -> tuple[datetime, bool] | None:
    """Return (sim_now, running) or None if clock row missing."""
    clock = (await db.execute(select(SimClock).where(SimClock.id == 1))).scalar_one_or_none()
    if clock is None:
        return None
    return clock.sim_now, bool(clock.running)


async def _is_vip(db: AsyncSession, v: Visita) -> bool:
    """Two paths to VIP: `visita.es_vip == 1` or `cliente.es_vip = True`."""
    if v.es_vip:
        return True
    if v.cliente_id is None:
        return False
    cli = (await db.execute(
        select(Cliente).where(Cliente.cliente_id == v.cliente_id)
    )).scalar_one_or_none()
    return bool(cli and cli.es_vip)


async def _process_visita(
    db: AsyncSession,
    *,
    v: Visita,
    sim_now: datetime,
    rng: random.Random,
) -> str:
    """Roll outcome, mutate `v`, append audit row. Returns new estado."""
    is_vip = await _is_vip(db, v)
    success_rate = (
        settings.sim_progression_success_rate_vip
        if is_vip
        else settings.sim_progression_success_rate_default
    )
    delivered = rng.random() < success_rate

    old_estado = v.estado
    if delivered:
        new_estado = "entregado"
        llegada_offset = rng.randint(2, 15)
        v.estado = new_estado
        v.llegada_at = sim_now - timedelta(minutes=llegada_offset)
        v.completada_at = sim_now
        v.motivo = None
        payload: dict = {
            "old": old_estado,
            "new": new_estado,
            "sim_now": sim_now.isoformat(),
            "source": "sim_progression",
            "vip": is_vip,
        }
    else:
        new_estado = "no_entregado"
        motivo = _pick_motivo(rng)
        llegada_offset = rng.randint(2, 10)
        v.estado = new_estado
        v.llegada_at = sim_now - timedelta(minutes=llegada_offset)
        v.completada_at = sim_now
        v.motivo = motivo
        payload = {
            "old": old_estado,
            "new": new_estado,
            "motivo": motivo,
            "sim_now": sim_now.isoformat(),
            "source": "sim_progression",
            "vip": is_vip,
        }

    await log_visita_evento(
        db,
        visita_id=v.visita_id,
        tipo="estado_change",
        user_id=None,
        payload=payload,
    )
    return new_estado


# ----------------------------------------------------------------------------
# Public job body.
# ----------------------------------------------------------------------------

async def auto_progression_job(rng: random.Random | None = None) -> dict[str, int]:
    """One tick of the cron. Returns counts for observability + tests.

    The `rng` parameter lets tests inject a seeded `random.Random` for
    deterministic outcomes. In production we use the module-level singleton.
    """
    rng = rng or _rng
    counts = {"delivered": 0, "failed": 0, "scanned": 0}
    sm = get_sessionmaker()
    try:
        async with sm() as db:
            clock = await _sim_now_running(db)
            if clock is None:
                return counts
            sim_now, running = clock
            if not running:
                return counts

            threshold = sim_now - timedelta(minutes=settings.sim_progression_grace_min)
            dias = (await db.execute(
                select(DiaOperativo).where(DiaOperativo.estado == "EN_CURSO")
            )).scalars().all()

            for dia in dias:
                visitas = (await db.execute(
                    select(Visita)
                    .where(
                        Visita.dia_id == dia.dia_id,
                        Visita.estado == "pendiente",
                        Visita.eta_estimada.isnot(None),
                        Visita.eta_estimada <= threshold,
                    )
                    .order_by(Visita.eta_estimada.asc())
                    .limit(settings.sim_progression_max_per_tick)
                )).scalars().all()

                for v in visitas:
                    counts["scanned"] += 1
                    new_estado = await _process_visita(
                        db, v=v, sim_now=sim_now, rng=rng,
                    )
                    if new_estado == "entregado":
                        counts["delivered"] += 1
                    else:
                        counts["failed"] += 1

            # Auto-close completed dias. A dia with visitas but zero pending is
            # finished: leaving it EN_CURSO meant the sim kept ticking its
            # drivers forever and it lingered in the operational view. We close
            # at the operational (sim) time. `closed` is logged only — NOT added
            # to `counts`, whose shape is asserted by tests.
            closed = 0
            closed_dias: list[DiaOperativo] = []
            for dia in dias:
                total = await db.scalar(
                    select(func.count()).select_from(Visita).where(Visita.dia_id == dia.dia_id)
                )
                pending = await db.scalar(
                    select(func.count()).select_from(Visita).where(
                        Visita.dia_id == dia.dia_id,
                        Visita.estado == "pendiente",
                    )
                )
                if total and pending == 0 and dia.estado == "EN_CURSO":
                    dia.estado = "CERRADO"
                    dia.cerrado_at = sim_now
                    closed += 1
                    closed_dias.append(dia)
                    logger.info(
                        f"[sim_progression] auto-closed dia {dia.dia_id} "
                        f"({total} visitas, 0 pending) at sim_now={sim_now.isoformat()}"
                    )

            if counts["scanned"] or closed:
                await db.commit()
                logger.info(
                    f"[sim_progression] tick scanned={counts['scanned']} "
                    f"delivered={counts['delivered']} failed={counts['failed']} "
                    f"closed={closed} sim_now={sim_now.isoformat()}"
                )
                # CR-3b: end-of-day report push for auto-closed dias (best-effort).
                for d in closed_dias:
                    try:
                        from app.core.report_push import push_dia_report
                        await push_dia_report(db, d)
                    except Exception:
                        logger.exception(f"[sim_progression] report push failed dia {d.dia_id}")
    except Exception:
        logger.exception("[sim_progression] tick failed")
    return counts


# ----------------------------------------------------------------------------
# Registration helper — called from app lifespan.
# ----------------------------------------------------------------------------

def setup_sim_progression_job(scheduler) -> None:
    """Register the cron on the given APScheduler instance.

    Off-switch: `settings.sim_progression_enabled = False` skips registration.
    """
    if not settings.sim_progression_enabled:
        logger.info("[sim_progression] disabled by config — not scheduled")
        return
    scheduler.add_job(
        auto_progression_job,
        trigger="interval",
        seconds=settings.sim_progression_interval_s,
        id="sim.auto_progression",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        f"[sim_progression] scheduled (interval={settings.sim_progression_interval_s}s, "
        f"grace={settings.sim_progression_grace_min}m, "
        f"max_per_tick={settings.sim_progression_max_per_tick}, "
        f"success_default={settings.sim_progression_success_rate_default:.0%}, "
        f"success_vip={settings.sim_progression_success_rate_vip:.0%})"
    )

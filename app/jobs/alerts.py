"""APScheduler jobs for alert generation (CR-022 Part A).

Three crons run against `sim_clock.sim_now` (the operational clock, not wall
time). They scan EN_CURSO dias, detect conditions, write `td.alerts` rows
with a `dedupe_key`, and dispatch via `alert_dispatcher.dispatch_alert`.

  * eta_breach_job     — every 5 min: visitas pending with eta < now - GRACE.
  * eta_preview_job    — every 5 min: visitas pending with now <= eta <= now + PREVIEW.
  * vip_deadline_job   — every 60s: VIP visitas pending with eta <= now + VIP_DEADLINE.

Each job opens its own session via `get_sessionmaker()` and wraps its body in
a global try/except so a transient SQL error doesn't crash the scheduler.

Dedupe is per-cron with a `dedupe_key` of `{tipo}:{visita_id}:{sim_date}`.
DB also has a filtered unique index on `dedupe_key WHERE estado != 'descartada'`
so even a race-on-create gets caught (we re-raise the IntegrityError as a
quiet skip).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_dispatcher import dispatch_alert
from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.sim_clock import SimClock
from app.db.models.visita import Visita
from app.db.session import get_sessionmaker


async def _sim_now(db: AsyncSession) -> datetime | None:
    """Read sim_clock.sim_now. None if no clock row exists yet."""
    clock = (await db.execute(select(SimClock).where(SimClock.id == 1))).scalar_one_or_none()
    return clock.sim_now if clock else None


async def _alert_exists(db: AsyncSession, dedupe_key: str) -> bool:
    """Has an alert with this dedupe_key been created and NOT dismissed?

    Mirrors the filtered unique index — descartada rows are excluded so
    operators can re-trigger detection after a dismissal.
    """
    stmt = select(Alert.alert_id).where(
        Alert.dedupe_key == dedupe_key,
        Alert.estado != "descartada",
    )
    return (await db.execute(stmt)).first() is not None


async def _create_and_dispatch(
    db: AsyncSession,
    *,
    tipo: str,
    severity: str,
    empresa_id: int,
    dia_id: int,
    visita: Visita,
    descripcion: str,
    payload: dict,
    dedupe_key: str,
) -> None:
    """Insert one alert + dispatch. Idempotent via dedupe pre-check."""
    if await _alert_exists(db, dedupe_key):
        return
    alert = Alert(
        tipo=tipo,
        severity=severity,
        empresa_id=empresa_id,
        dia_id=dia_id,
        ruta_id=visita.ruta_id,
        visita_id=visita.visita_id,
        descripcion=descripcion,
        payload_json=json.dumps(payload, default=str),
        estado="abierta",
        dedupe_key=dedupe_key,
    )
    db.add(alert)
    try:
        await db.commit()
    except IntegrityError:
        # Race: another worker created the same dedupe_key between our
        # SELECT and INSERT. Roll back and skip.
        await db.rollback()
        logger.debug(f"[alerts] dedupe collision on {dedupe_key}, skipping")
        return
    await db.refresh(alert)
    await dispatch_alert(db, alert, motivo=visita.motivo)


# ----------------------------------------------------------------------------
# Job bodies — each opens its own session and is self-contained.
# ----------------------------------------------------------------------------

async def eta_breach_job() -> None:
    """For each EN_CURSO dia, alert visitas whose ETA expired > GRACE min ago."""
    sm = get_sessionmaker()
    grace = settings.alerts_grace_min
    try:
        async with sm() as db:
            sim_now = await _sim_now(db)
            if sim_now is None:
                return
            threshold = sim_now - timedelta(minutes=grace)
            dias = (await db.execute(
                select(DiaOperativo).where(DiaOperativo.estado == "EN_CURSO")
            )).scalars().all()
            for dia in dias:
                visitas = (await db.execute(
                    select(Visita).where(
                        Visita.dia_id == dia.dia_id,
                        Visita.estado == "pendiente",
                        Visita.eta_estimada.isnot(None),
                        Visita.eta_estimada < threshold,
                    )
                )).scalars().all()
                for v in visitas:
                    delta_min = int((sim_now - v.eta_estimada).total_seconds() / 60)
                    dedupe = f"eta_breach:{v.visita_id}:{sim_now.date().isoformat()}"
                    # CR-024 — VIP / prioridad escalation. Lookup the cliente
                    # row when the visita references one; default to 'alta',
                    # escalate to 'critica' if cliente.es_vip or cliente.prioridad <= 2.
                    severity = "alta"
                    if v.cliente_id is not None:
                        cli = (await db.execute(
                            select(Cliente).where(Cliente.cliente_id == v.cliente_id)
                        )).scalar_one_or_none()
                        if cli is not None and (
                            bool(cli.es_vip)
                            or (cli.prioridad is not None and cli.prioridad <= 2)
                        ):
                            severity = "critica"
                            logger.info(
                                f"[eta_breach] visita {v.visita_id} "
                                f"cliente VIP/prio<=2 → severity escalada a critica"
                            )
                    desc = (
                        f"Visita #{v.orden} folio {v.folio_cliente or '-'} "
                        f"con {delta_min} min de atraso"
                    )
                    await _create_and_dispatch(
                        db,
                        tipo="eta_breach",
                        severity=severity,
                        empresa_id=v.empresa_id,
                        dia_id=dia.dia_id,
                        visita=v,
                        descripcion=desc,
                        payload={
                            "eta_estimada": v.eta_estimada.isoformat(),
                            "sim_now": sim_now.isoformat(),
                            "delta_min": delta_min,
                            "grace_min": grace,
                        },
                        dedupe_key=dedupe,
                    )
    except Exception:
        logger.exception("[alerts] eta_breach_job failed")


async def eta_preview_job() -> None:
    """Pre-aviso PREVIEW_MIN before ETA fires."""
    sm = get_sessionmaker()
    preview = settings.alerts_preview_min
    try:
        async with sm() as db:
            sim_now = await _sim_now(db)
            if sim_now is None:
                return
            upper = sim_now + timedelta(minutes=preview)
            dias = (await db.execute(
                select(DiaOperativo).where(DiaOperativo.estado == "EN_CURSO")
            )).scalars().all()
            for dia in dias:
                visitas = (await db.execute(
                    select(Visita).where(
                        Visita.dia_id == dia.dia_id,
                        Visita.estado == "pendiente",
                        Visita.eta_estimada.isnot(None),
                        Visita.eta_estimada >= sim_now,
                        Visita.eta_estimada <= upper,
                    )
                )).scalars().all()
                for v in visitas:
                    eta_min = int((v.eta_estimada - sim_now).total_seconds() / 60)
                    dedupe = f"eta_preview:{v.visita_id}:{sim_now.date().isoformat()}"
                    desc = (
                        f"Visita #{v.orden} folio {v.folio_cliente or '-'} "
                        f"en {eta_min} min"
                    )
                    await _create_and_dispatch(
                        db,
                        tipo="eta_preview",
                        severity="media",
                        empresa_id=v.empresa_id,
                        dia_id=dia.dia_id,
                        visita=v,
                        descripcion=desc,
                        payload={
                            "eta_estimada": v.eta_estimada.isoformat(),
                            "sim_now": sim_now.isoformat(),
                            "eta_in_min": eta_min,
                            "preview_min": preview,
                        },
                        dedupe_key=dedupe,
                    )
    except Exception:
        logger.exception("[alerts] eta_preview_job failed")


async def vip_deadline_job() -> None:
    """VIP visitas pendientes con ETA dentro de VIP_DEADLINE min."""
    sm = get_sessionmaker()
    deadline = settings.alerts_vip_deadline_min
    try:
        async with sm() as db:
            sim_now = await _sim_now(db)
            if sim_now is None:
                return
            upper = sim_now + timedelta(minutes=deadline)
            dias = (await db.execute(
                select(DiaOperativo).where(DiaOperativo.estado == "EN_CURSO")
            )).scalars().all()
            for dia in dias:
                # Two paths to VIP: visita.es_vip == 1 or cliente.es_vip == True.
                # We OR them in Python because LEFT JOIN + filter on either is
                # awkward in SQLAlchemy 2.0 selects; the result sets are small.
                visitas = (await db.execute(
                    select(Visita).where(
                        Visita.dia_id == dia.dia_id,
                        Visita.estado == "pendiente",
                        Visita.eta_estimada.isnot(None),
                        # Window is [sim_now, sim_now + deadline]: a VIP whose ETA
                        # is still APPROACHING. Without the lower bound, a VIP
                        # already hours overdue kept firing "deadline próximo"
                        # (overdue VIPs are covered by eta_breach instead).
                        Visita.eta_estimada >= sim_now,
                        Visita.eta_estimada <= upper,
                    )
                )).scalars().all()
                for v in visitas:
                    is_vip = bool(v.es_vip)
                    if not is_vip and v.cliente_id is not None:
                        cli = (await db.execute(
                            select(Cliente).where(Cliente.cliente_id == v.cliente_id)
                        )).scalar_one_or_none()
                        is_vip = bool(cli and cli.es_vip)
                    if not is_vip:
                        continue
                    dedupe = f"vip_deadline:{v.cliente_id or 0}:{v.visita_id}"
                    desc = (
                        f"VIP visita #{v.orden} folio {v.folio_cliente or '-'} "
                        f"con deadline próximo"
                    )
                    await _create_and_dispatch(
                        db,
                        tipo="vip_deadline",
                        severity="critica",
                        empresa_id=v.empresa_id,
                        dia_id=dia.dia_id,
                        visita=v,
                        descripcion=desc,
                        payload={
                            "eta_estimada": v.eta_estimada.isoformat(),
                            "sim_now": sim_now.isoformat(),
                            "deadline_min": deadline,
                            "cliente_id": v.cliente_id,
                        },
                        dedupe_key=dedupe,
                    )
    except Exception:
        logger.exception("[alerts] vip_deadline_job failed")


# ----------------------------------------------------------------------------
# Registration helper — called from app lifespan.
# ----------------------------------------------------------------------------

def setup_alert_jobs(scheduler) -> None:
    """Register the 3 jobs on the given APScheduler instance.

    Job IDs are stable (`alerts.<tipo>`) so a hot-reload during dev replaces
    them in place rather than stacking duplicates.
    """
    scheduler.add_job(
        eta_breach_job,
        trigger="interval",
        minutes=5,
        id="alerts.eta_breach",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        eta_preview_job,
        trigger="interval",
        minutes=5,
        id="alerts.eta_preview",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        vip_deadline_job,
        trigger="interval",
        seconds=60,
        id="alerts.vip_deadline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        f"[alerts] scheduled 3 cron jobs (grace={settings.alerts_grace_min}m, "
        f"preview={settings.alerts_preview_min}m, vip_deadline={settings.alerts_vip_deadline_min}m)"
    )

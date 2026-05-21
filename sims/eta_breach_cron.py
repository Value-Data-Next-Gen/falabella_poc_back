"""Cron de chequeo de ETA breaches (Fase 3 MVP).

Reemplaza al viejo `_auto_notify_alerts` que vivia en state.py y dependia del
snapshot ML eliminado en Fase 2. La logica ahora es directa contra DB:

  Cada 5 minutos:
    1) Si el dia operativo de hoy NO esta EN_CURSO -> early return.
    2) sim_clock_now = get_sim_clock(date.today())
    3) SELECT visitas con planned_date = hoy, status = 'pending',
       current_eta_cl < (sim_clock_now - GRACE_MIN).
    4) Para cada una que no haya sido alertada todavia hoy, dispatchea
       template `vd_alerta_motivo_v2` via la funcion reusable
       `routers.admin_day_notifications.dispatch_eta_breach`.

Cache in-memory de tracking_ids alertados (`{fecha_iso: set(ids)}`) para evitar
re-alertar al driver cada 5 min mientras la visita siga pending vencida.
Si el proceso se reinicia el cache se pierde y se re-alerta — aceptable para
el POC.

Registrado desde main.py:lifespan con APScheduler interval 300s.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

from core.db import get_conn
from core.state import get_sim_clock, is_operational_day_active


JOB_ID = "eta-breach-cron"
GRACE_MINUTES = 10        # margen de cortesia antes de declarar atraso
INTERVAL_SECONDS = 300    # cada 5 min


# Cache in-memory: { 'YYYY-MM-DD': set(tracking_ids) }.
# Evita re-alertar al mismo driver cada 5 min mientras la visita siga
# pending vencida. Se resetea al cambiar de dia operativo (se purgan
# entries de fechas != hoy).
_alerted: dict[str, set[str]] = {}


def _purge_old_keys(today_iso: str) -> None:
    """Limpia entradas de fechas != hoy del cache."""
    stale = [k for k in _alerted if k != today_iso]
    for k in stale:
        _alerted.pop(k, None)


def _today_iso() -> str:
    return date.today().isoformat()


def check_eta_breaches() -> dict:
    """Funcion publica del cron. Devuelve resumen para logs / health."""
    today_iso = _today_iso()
    _purge_old_keys(today_iso)

    # Gate: solo corremos si el dia esta EN_CURSO. En BORRADOR/VALIDADO/CERRADO
    # no tiene sentido alertar (el operador no esta usando la torre).
    if not is_operational_day_active():
        return {"checked": 0, "fired": 0, "skipped": "day_not_en_curso"}

    sim_clock = get_sim_clock(today_iso)
    threshold = sim_clock - timedelta(minutes=GRACE_MINUTES)

    # Query: visitas pending con ETA vencida.
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT v.id, v.title, v.comuna, v.current_eta_cl, v.patente_falsa,
                       d.driver_id, d.name AS driver_name, d.phone_e164,
                       d.notify_whatsapp, d.opted_in_at
                FROM fpoc.simpli_visits v
                JOIN fpoc.drivers d ON d.vehicle_id = v.patente_falsa
                WHERE v.planned_date = ?
                  AND LOWER(v.status) = 'pending'
                  AND v.current_eta_cl < ?
                  AND d.active = 1
                """,
                today_iso, threshold,
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[eta-breach] query fallo: {e}")
        return {"checked": 0, "fired": 0, "error": str(e)}

    alerted_today = _alerted.setdefault(today_iso, set())
    fired = 0
    skipped = 0
    checked = len(rows)

    # Import lazy para evitar circulares en boot.
    from routers.admin_day_notifications import dispatch_eta_breach

    for r in rows:
        tid = str(r.id)
        if tid in alerted_today:
            skipped += 1
            continue
        # Pre-validacion: driver debe tener phone E164 + notify + opt-in.
        # dispatch_eta_breach ya valida esto pero rebienta con HTTPException;
        # acá filtramos antes para no llenar de excepciones el log.
        phone = (str(r.phone_e164) if r.phone_e164 else "").strip()
        notify = bool(r.notify_whatsapp) if r.notify_whatsapp is not None else False
        opted_in: Optional[datetime] = r.opted_in_at
        if not phone or not phone.startswith("+") or not notify or opted_in is None:
            # Igual lo marcamos como alertado para no spamear chequeos vacios.
            alerted_today.add(tid)
            skipped += 1
            continue
        try:
            resp = dispatch_eta_breach(tid, triggered_by="eta_breach_auto")
            alerted_today.add(tid)
            if resp.status in ("queued", "sent", "dry_run"):
                fired += 1
            else:
                skipped += 1
                logger.warning(
                    f"[eta-breach] dispatch TID={tid} status={resp.status} "
                    f"error={resp.error}"
                )
        except Exception as e:  # noqa: BLE001
            # No marcamos como alertado para reintentar el ciclo siguiente.
            logger.warning(f"[eta-breach] dispatch TID={tid} excepcion: {e}")
            skipped += 1

    if fired:
        logger.info(
            f"[eta-breach] {today_iso} sim_clock={sim_clock.strftime('%H:%M')} "
            f"checked={checked} fired={fired} skipped={skipped}"
        )
    return {"checked": checked, "fired": fired, "skipped": skipped}


def register_cron(scheduler) -> None:
    """Registra el job en el scheduler dado."""
    scheduler.add_job(
        check_eta_breaches,
        "interval",
        seconds=INTERVAL_SECONDS,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    logger.info(f"[eta-breach] scheduler started (interval {INTERVAL_SECONDS}s)")

"""Cron de pre-aviso ETA (Pieza #2 del loop operativo).

Cada 5 minutos chequea visitas pending con ETA en los próximos 10-20 min.
Para cada una, manda freeform al driver:

    📍 Próxima visita en ~15min:
    {cliente} ({comuna})
    Hora estimada: {HH:MM}
    Si saliste tarde, mandá '3' para reportar atraso ahora.

Es complementario a `eta_breach_cron` (que dispara DESPUÉS de la ETA vencida):
este cron dispara ANTES, como recordatorio amistoso. Ayuda al driver a
priorizar mentalmente la siguiente entrega sin esperar a que ya esté
atrasado.

Gates:
  - Solo si is_operational_day_active() (día EN_CURSO).
  - Solo a drivers opted-in con notify_whatsapp=1.
  - Cache in-memory `_previewed = {fecha: set(tids)}` para no repetir
    el mismo TID el siguiente ciclo.

Registrado desde main.py:lifespan con APScheduler interval 300s.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

from core.db import get_conn
from core.state import get_sim_clock, is_operational_day_active


JOB_ID = "eta-preview-cron"
PREVIEW_WINDOW_MIN_FROM = 10  # ventana inicia +10min del sim_clock
PREVIEW_WINDOW_MIN_TO = 20    # ventana termina +20min
INTERVAL_SECONDS = 300         # cada 5 min


_previewed: dict[str, set[str]] = {}


def _purge_old_keys(today_iso: str) -> None:
    stale = [k for k in _previewed if k != today_iso]
    for k in stale:
        _previewed.pop(k, None)


def check_eta_previews() -> dict:
    """Funcion publica del cron. Devuelve resumen para logs / health."""
    today_iso = date.today().isoformat()
    _purge_old_keys(today_iso)

    if not is_operational_day_active():
        return {"checked": 0, "fired": 0, "skipped": "day_not_en_curso"}

    sim_clock = get_sim_clock(today_iso)
    window_start = sim_clock + timedelta(minutes=PREVIEW_WINDOW_MIN_FROM)
    window_end = sim_clock + timedelta(minutes=PREVIEW_WINDOW_MIN_TO)

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
                  AND v.current_eta_cl >= ?
                  AND v.current_eta_cl <= ?
                  AND d.active = 1
                ORDER BY v.patente_falsa, v.current_eta_cl ASC
                """,
                today_iso, window_start, window_end,
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[eta-preview] query fallo: {e}")
        return {"checked": 0, "fired": 0, "error": str(e)}

    previewed_today = _previewed.setdefault(today_iso, set())
    fired = 0
    skipped = 0
    checked = len(rows)

    # Agrupar por driver: 1 preview por driver por ciclo (el más próximo).
    by_driver: dict = {}
    for r in rows:
        by_driver.setdefault(int(r.patente_falsa), []).append(r)

    from routers.notifications import send_whatsapp

    for patente, driver_rows in by_driver.items():
        target_row = None
        for r in driver_rows:
            tid = str(r.id)
            if tid not in previewed_today:
                target_row = r
                break
        if target_row is None:
            skipped += len(driver_rows)
            continue

        tid = str(target_row.id)
        phone = (str(target_row.phone_e164) if target_row.phone_e164 else "").strip()
        notify = bool(target_row.notify_whatsapp) if target_row.notify_whatsapp is not None else False
        opted_in: Optional[datetime] = target_row.opted_in_at
        if not phone or not phone.startswith("+") or not notify or opted_in is None:
            previewed_today.add(tid)
            skipped += 1
            continue

        cliente = str(target_row.title or "—")
        comuna = str(target_row.comuna or "")
        eta = target_row.current_eta_cl
        eta_str = eta.strftime("%H:%M") if hasattr(eta, "strftime") else str(eta)[11:16]
        cliente_label = f"{cliente}" + (f" ({comuna})" if comuna else "")

        body = (
            f"📍 *Próxima visita en ~15 min*:\n"
            f"{cliente_label}\n"
            f"ETA: *{eta_str}*\n\n"
            f"Si vas demorado, mandá '3' para reportar el atraso ahora."
        )

        try:
            send_whatsapp(
                body=body,
                targets=[(None, phone)],
                subject=f"Próxima visita · {cliente}",
                tracking_id=tid,
                triggered_by="eta_preview_auto",
            )
            previewed_today.add(tid)
            fired += 1
            others = len(driver_rows) - 1
            if others > 0:
                skipped += others
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[eta-preview] TID={tid} envio fallo: {e}")
            skipped += 1

    if fired:
        logger.info(
            f"[eta-preview] {today_iso} sim_clock={sim_clock.strftime('%H:%M')} "
            f"checked={checked} fired={fired} skipped={skipped}"
        )
    return {"checked": checked, "fired": fired, "skipped": skipped}


def register_cron(scheduler) -> None:
    """Registra el job en el scheduler dado."""
    scheduler.add_job(
        check_eta_previews,
        "interval",
        seconds=INTERVAL_SECONDS,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    logger.info(f"[eta-preview] scheduler started (interval {INTERVAL_SECONDS}s)")

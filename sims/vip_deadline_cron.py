"""Cron VIP deadline checker.

Cada 60s:
  - Carga sim_clock actual desde STATE
  - Para cada VIP activa con deadline_time IS NOT NULL:
      - Encuentra visitas pending del día actual que matcheen el VIP
      - Si now >= (deadline_time - alert_minutes_before)
        Y (last_alert_sent_at IS NULL O last_alert_sent_at < hoy):
          - emite evento `vip_deadline_warning`
          - dispara WhatsApp via dispatcher unificado (admin + manager empresa
            + contactos opt-in con filtros)
          - update fpoc_vip_clients.last_alert_sent_at = now()

Registrado desde main.py:lifespan con APScheduler interval 60s (jobs adicional al
existente, mismo scheduler).
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from typing import Optional

from loguru import logger

from routers.comments import _visit_region, _resolve_alert_targets
from core.db import get_conn
from core.events import EVENTS
from core.state import STATE


JOB_ID = "vip-deadline-cron"


def _parse_hhmm(value: str, base_date: date) -> Optional[datetime]:
    try:
        h, m = value.split(":")
        return datetime.combine(base_date, time(int(h), int(m)))
    except Exception:  # noqa: BLE001
        return None


def _matching_visits_for_vip(df, vip: dict) -> list:
    """Devuelve lista de filas (dict-like) del snapshot que matchean al VIP."""
    mt = vip["match_type"]
    mv = vip["match_value"]
    eid = vip.get("empresa_id")
    sub = df
    # Scope por empresa si VIP no es global
    if eid is not None:
        sub = sub[sub["empresa_id"] == eid]
    if mt == "title":
        sub = sub[sub["title"].astype(str) == str(mv)]
    elif mt == "customer_id":
        # snapshot_df no siempre trae customer_id; best-effort por columna si existe
        if "customer_id" in sub.columns:
            sub = sub[sub["customer_id"].astype(str) == str(mv)]
        else:
            return []
    elif mt == "reference":
        if "reference" in sub.columns:
            sub = sub[sub["reference"].astype(str) == str(mv)]
        else:
            return []
    return [row for _, row in sub.iterrows()]


def _last_alert_is_today(last_str: Optional[str], today: date) -> bool:
    if not last_str:
        return False
    try:
        # SQLite stores as string "YYYY-MM-DD HH:MM:SS"
        if "T" in last_str:
            d = datetime.fromisoformat(last_str)
        else:
            d = datetime.fromisoformat(last_str.replace(" ", "T"))
        return d.date() == today
    except Exception:  # noqa: BLE001
        return False


def _build_warning_body(*, cliente: str, deadline: str, mins_left: int,
                       plate: Optional[str], transporte: Optional[str],
                       tracking_id: str, eta: str, slack: float) -> str:
    veh_line = transporte or "—"
    if plate:
        veh_line = f"{plate} ({transporte or '—'})"
    return (
        f"⏰ ALERTA VIP DEADLINE\n"
        f"Cliente: {cliente}\n"
        f"Llegar antes de: {deadline}\n"
        f"Quedan: {mins_left} min\n"
        f"Vehículo: {veh_line}\n"
        f"ETA actual: {eta or '—'}  ·  Slack: {slack:+.0f} min\n"
        f"Tracking: {tracking_id}"
    )


_VIP_SCHEMA_FIXED = False


def _ensure_vip_columns() -> None:
    """Auto-migracion idempotente: agrega deadline_time, alert_minutes_before
    y last_alert_sent_at a fpoc.vip_clients si faltan (Azure SQL solamente).

    Corre una sola vez por proceso (flag _VIP_SCHEMA_FIXED).
    """
    global _VIP_SCHEMA_FIXED
    if _VIP_SCHEMA_FIXED:
        return
    try:
        from core.db import backend
        if backend() != "sqlserver":
            _VIP_SCHEMA_FIXED = True
            return
        cols = [
            ("deadline_time", "NVARCHAR(8) NULL"),
            ("alert_minutes_before", "INT NOT NULL CONSTRAINT DF_vip_alert_min DEFAULT 60 WITH VALUES"),
            ("last_alert_sent_at", "DATETIME2 NULL"),
        ]
        with get_conn() as cn:
            cur = cn.cursor()
            added: list[str] = []
            for col, ddl in cols:
                cur.execute(
                    "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = 'vip_clients' "
                    "AND COLUMN_NAME = ?",
                    (col,),
                )
                if cur.fetchone():
                    continue
                cur.execute(f"ALTER TABLE [fpoc].[vip_clients] ADD {col} {ddl}")
                added.append(col)
            cn.commit()
            if added:
                logger.info(f"[vip-deadline] auto-migracion OK, columnas agregadas: {added}")
        _VIP_SCHEMA_FIXED = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vip-deadline] auto-migracion fallo (reintenta despues): {e}")


def check_vip_deadlines() -> dict:
    """Función pública del cron. Devuelve resumen para logs/health."""
    _ensure_vip_columns()
    if STATE.snapshot_df is None or STATE.sim_clock is None or STATE.today is None:
        return {"checked": 0, "fired": 0, "skipped_warmup": True}

    sim_clock: datetime = STATE.sim_clock
    today: date = STATE.today

    df = STATE.snapshot_df.copy()
    df["empresa_id"] = df["vehicle_id"].astype(int).map(STATE.vehicle_empresa_map)

    vips: list[dict] = []
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT vip_id, match_type, match_value, empresa_id, tier,
                       deadline_time, alert_minutes_before, last_alert_sent_at
                FROM fpoc.vip_clients
                WHERE active = 1 AND deadline_time IS NOT NULL
                """,
            )
            for r in cur.fetchall():
                last = r.last_alert_sent_at
                last_str = last.isoformat() if hasattr(last, "isoformat") else (str(last) if last else None)
                vips.append({
                    "vip_id": int(r.vip_id),
                    "match_type": r.match_type,
                    "match_value": r.match_value,
                    "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
                    "tier": r.tier,
                    "deadline_time": str(r.deadline_time),
                    "alert_minutes_before": int(r.alert_minutes_before) if r.alert_minutes_before is not None else 60,
                    "last_alert_sent_at": last_str,
                })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vip-deadline] no pude cargar VIPs: {e}")
        return {"checked": 0, "fired": 0, "error": str(e)}

    fired = 0
    checked = 0
    plate_by_vid = {int(v["vehicle_id"]): v.get("plate") for v in STATE.vehicles_ext}
    empresas_cat = {int(e["empresa_id"]): e["nombre"] for e in STATE.empresas}

    for vip in vips:
        # Si ya alertamos hoy, skip
        if _last_alert_is_today(vip["last_alert_sent_at"], today):
            continue

        deadline_dt = _parse_hhmm(vip["deadline_time"], today)
        if deadline_dt is None:
            continue
        alert_dt = deadline_dt - timedelta(minutes=vip["alert_minutes_before"])

        if sim_clock < alert_dt:
            continue
        # Pasamos el umbral. Buscar visitas pending matching, no completadas.
        matches = _matching_visits_for_vip(df, vip)
        # solo pending
        matches = [r for r in matches if str(r["status"]) == "pending"]
        if not matches:
            continue

        checked += 1
        mins_left = max(0, int((deadline_dt - sim_clock).total_seconds() // 60))

        for row in matches:
            tid = str(row["tracking_id"])
            cliente = str(row["title"])
            vehicle_id = int(row["vehicle_id"])
            plate = plate_by_vid.get(vehicle_id)
            vehicle_name = str(row.get("vehicle_name", ""))
            empresa_id = STATE.vehicle_empresa_map.get(vehicle_id) if vip["empresa_id"] is None else vip["empresa_id"]
            eta = str(row.get("estimated_time_arrival", ""))[:5]
            slack = float(row.get("slack_min", 0.0))

            # Evento siempre
            EVENTS.emit("vip_deadline_warning", sim_clock, {
                "tracking_id": tid,
                "vehicle_id": vehicle_id,
                "vehicle_name": vehicle_name,
                "title": cliente,
                "deadline_time": vip["deadline_time"],
                "minutes_remaining": mins_left,
                "tier": vip["tier"],
                "empresa_nombre": empresas_cat.get(int(empresa_id)) if empresa_id is not None else None,
            })

            # WhatsApp via dispatcher unificado (gated por ENABLE_AUTO_NOTIFY)
            if os.environ.get("ENABLE_AUTO_NOTIFY", "false").lower() == "true":
                try:
                    from routers.notifications import send_whatsapp
                    body = _build_warning_body(
                        cliente=cliente,
                        deadline=vip["deadline_time"],
                        mins_left=mins_left,
                        plate=plate,
                        transporte=vehicle_name,
                        tracking_id=tid,
                        eta=eta,
                        slack=slack,
                    )
                    visit_region = _visit_region(row.get("latitude"), row.get("longitude"))
                    targets, contact_ids_by_phone = _resolve_alert_targets(
                        empresa_id=empresa_id,
                        severity="critical",
                        motivo="VIP_DEADLINE",
                        visit_region=visit_region,
                    )
                    if targets:
                        send_whatsapp(
                            body=body,
                            targets=targets,
                            subject=f"VIP deadline {vip['deadline_time']} · {cliente}",
                            tracking_id=tid,
                            triggered_by="vip_deadline",
                        )
                        # backfill de contact_id (best-effort)
                        try:
                            from routers.comments import _backfill_contact_id_in_log
                            _backfill_contact_id_in_log(
                                tracking_id=tid,
                                contact_ids_by_phone=contact_ids_by_phone,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[vip-deadline] envío WhatsApp falló para {tid}: {e}")
            fired += 1

        # Update last_alert_sent_at una vez por VIP (no por visita)
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "UPDATE fpoc.vip_clients SET last_alert_sent_at = CURRENT_TIMESTAMP WHERE vip_id = ?",
                    vip["vip_id"],
                )
                cn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[vip-deadline] no pude actualizar last_alert_sent_at: {e}")

    if fired:
        logger.info(f"[vip-deadline] disparadas {fired} alertas en {checked} VIPs")
    return {"checked": checked, "fired": fired}


def register_cron(scheduler) -> None:
    """Registra el job en el scheduler dado (compartido con sim-tick)."""
    scheduler.add_job(
        check_vip_deadlines,
        "interval",
        seconds=60,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[vip-deadline] scheduler started (interval 60s)")

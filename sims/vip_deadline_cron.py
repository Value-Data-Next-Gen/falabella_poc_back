"""Cron VIP deadline checker (post Fase-2 MVP refactor).

Cada 60s:
  - Carga sim_clock actual desde STATE (fallback UTC now)
  - Para cada VIP activa con deadline_time IS NOT NULL:
      - Encuentra visitas pending de la fecha activa que matcheen el VIP
        (match_type=title|customer_id|reference) — consultando fpoc.simpli_visits
      - Si sim_clock >= (deadline_time - alert_minutes_before)
        Y (last_alert_sent_at IS NULL O last_alert_sent_at < hoy):
          - emite evento `vip_deadline_warning`
          - dispara WhatsApp via dispatcher unificado (admin + manager empresa
            + contactos opt-in con filtros)
          - update fpoc.vip_clients.last_alert_sent_at = now()

Tras Fase 2: ya no leemos STATE.snapshot_df (eliminado junto con el modelo ML).
La fuente es la tabla real `fpoc.simpli_visits`.

Registrado desde main.py:lifespan con APScheduler interval 60s.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from typing import Optional

from loguru import logger

from routers.comments import _visit_region, _resolve_alert_targets, _sanitize_template_var
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


def _last_alert_is_today(last_str: Optional[str], today: date) -> bool:
    if not last_str:
        return False
    try:
        if "T" in last_str:
            d = datetime.fromisoformat(last_str)
        else:
            d = datetime.fromisoformat(last_str.replace(" ", "T"))
        return d.date() == today
    except Exception:  # noqa: BLE001
        return False


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


def _matching_visits_for_vip(today_iso: str, vip: dict) -> list[dict]:
    """Lee fpoc.simpli_visits para encontrar visitas pending que matcheen al VIP.

    Match types soportados (consistente con fpoc.vip_clients.match_type):
      - "title"       → simpli_visits.title
      - "customer_id" → fpoc.simpli_visits no tiene customer_id; devolvemos []
      - "reference"   → simpli_visits.reference

    Scope:
      - planned_date = today_iso
      - status = 'pending'
      - empresa_falsa = vip.empresa_id (si VIP no es global)
    """
    mt = vip["match_type"]
    mv = vip["match_value"]
    eid = vip.get("empresa_id")

    if mt == "title":
        column = "title"
    elif mt == "reference":
        column = "reference"
    else:
        # customer_id u otro no soportado en simpli_visits
        return []

    sql_parts = [
        f"SELECT id, title, reference, comuna, region, address,",
        "       patente_falsa, driver_name, empresa_falsa, current_eta_cl",
        "FROM fpoc.simpli_visits",
        f"WHERE planned_date = ? AND status = 'pending' AND {column} = ?",
    ]
    params: list = [today_iso, str(mv)]
    if eid is not None:
        sql_parts.append("AND empresa_falsa = ?")
        params.append(eid)

    sql = "\n".join(sql_parts)
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(sql, *params)
            return [
                {
                    "id": str(r.id),
                    "title": r.title or "",
                    "reference": r.reference,
                    "comuna": r.comuna,
                    "region": r.region,
                    "address": r.address,
                    "patente_falsa": int(r.patente_falsa) if r.patente_falsa is not None else None,
                    "driver_name": r.driver_name,
                    "empresa_falsa": int(r.empresa_falsa) if r.empresa_falsa is not None else None,
                    "current_eta_cl": str(r.current_eta_cl) if r.current_eta_cl else None,
                }
                for r in cur.fetchall()
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vip-deadline] query visits falló (vip_id={vip.get('vip_id')}): {e}")
        return []


def _eta_to_hhmm(eta_raw: Optional[str]) -> str:
    if not eta_raw:
        return "—"
    s = str(eta_raw)
    if " " in s:
        return s.split(" ", 1)[1][:5]
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s[:5]


def _mins_remaining(deadline_dt: datetime, sim_clock: datetime) -> int:
    delta = (deadline_dt - sim_clock).total_seconds() // 60
    return max(0, int(delta))


def _build_warning_body(*, cliente: str, deadline: str, mins_left: int,
                       plate: Optional[str], transporte: Optional[str],
                       tracking_id: str, eta: str) -> str:
    veh_line = transporte or "—"
    if plate:
        veh_line = f"{plate} ({transporte or '—'})"
    return (
        f"ALERTA VIP DEADLINE\n"
        f"Cliente: {cliente}\n"
        f"Llegar antes de: {deadline}\n"
        f"Quedan: {mins_left} min\n"
        f"Vehiculo: {veh_line}\n"
        f"ETA actual: {eta or '—'}\n"
        f"Tracking: {tracking_id}"
    )


def check_vip_deadlines() -> dict:
    """Función pública del cron. Devuelve resumen para logs/health."""
    _ensure_vip_columns()

    sim_clock: datetime = STATE.sim_clock or datetime.utcnow()
    today: date = STATE.today or date.today()
    today_iso = today.isoformat()

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

        matches = _matching_visits_for_vip(today_iso, vip)
        if not matches:
            continue

        checked += 1
        mins_left = _mins_remaining(deadline_dt, sim_clock)

        for row in matches:
            tid = row["id"]
            cliente = row["title"]
            vehicle_id = row["patente_falsa"]
            plate = plate_by_vid.get(int(vehicle_id)) if vehicle_id is not None else None
            vehicle_name = f"PAT-{vehicle_id}" if vehicle_id is not None else "—"
            empresa_id = row["empresa_falsa"] if vip["empresa_id"] is None else vip["empresa_id"]
            eta = _eta_to_hhmm(row["current_eta_cl"])

            # Evento siempre (sirve al panel de Alertas Vivo)
            EVENTS.emit("vip_deadline_warning", sim_clock, {
                "tracking_id": tid,
                "vehicle_id": int(vehicle_id) if vehicle_id is not None else None,
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
                    )
                    # _visit_region acepta (lat, lon) o (lat, lon, region_hint).
                    # No tenemos lat/lon discretos acá; usamos region directo.
                    region_hint = (row.get("region") or "regiones")
                    visit_region = "RM" if region_hint == "RM" else "regiones"
                    targets, contact_ids_by_phone = _resolve_alert_targets(
                        empresa_id=empresa_id,
                        severity="critical",
                        motivo="VIP_DEADLINE",
                        visit_region=visit_region,
                    )
                    if targets:
                        subject_line = f"VIP deadline {vip['deadline_time']} · {cliente}"
                        from core.twilio_templates import vip_deadline_sid
                        content_sid = vip_deadline_sid()
                        content_variables = {
                            "1": _sanitize_template_var(cliente) or "—",
                            "2": _sanitize_template_var(vip['deadline_time']) or "—",
                            "3": _sanitize_template_var(mins_left) or "0",
                            "4": _sanitize_template_var(f"{plate or '—'} ({vehicle_name or '—'})") or "—",
                            "5": _sanitize_template_var(eta) or "—",
                            "6": _sanitize_template_var("0") or "0",  # slack obsoleto post-ML
                        }
                        used_template = False
                        if content_sid:
                            try:
                                send_whatsapp(
                                    content_sid=content_sid,
                                    content_variables=content_variables,
                                    targets=targets,
                                    subject=subject_line,
                                    tracking_id=tid,
                                    triggered_by="vip_deadline",
                                )
                                used_template = True
                            except Exception as e:  # noqa: BLE001
                                logger.warning(f"[vip-deadline] template vd_vip_deadline_v2 falló, fallback freeform: {e}")
                        if not used_template:
                            send_whatsapp(
                                body=body,
                                targets=targets,
                                subject=subject_line,
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
    """Registra el job en el scheduler dado."""
    scheduler.add_job(
        check_vip_deadlines,
        "interval",
        seconds=60,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[vip-deadline] scheduler started (interval 60s)")

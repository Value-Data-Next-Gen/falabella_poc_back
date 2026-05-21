"""Watchlist: visitas pending scoreadas por urgencia (post Fase-2 MVP refactor).

Tras eliminar el modelo XGB + STATE.snapshot_df, el watchlist lee directo de
`fpoc.simpli_visits` y computa urgencia simple basada en `current_eta_cl` vs
sim_clock (o UTC now si STATE no tiene reloj seteado).

Severity:
  URGENT   → eta vencida (<= now)
  WARNING  → eta dentro de los próximos 30 min
  OK       → eta > now + 30 min   (se excluye del watchlist)

VIP get +severity_bump y se marcan visualmente.

Scope: transport_manager ve solo visitas de su empresa.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn
from core.state import STATE

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class NotifInline(BaseModel):
    count: int
    sent_count: int
    last_status: str
    last_created_at: str


class WatchlistVisit(BaseModel):
    tracking_id: str
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    driver_name: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    title: str
    address: Optional[str] = None
    comuna: Optional[str] = None
    region: str
    estimated_time_arrival: Optional[str] = None
    status_label: str
    is_vip: bool
    vip_tier: Optional[str] = None
    vip_deadline_time: Optional[str] = None
    priority: str
    urgency_score: float
    severity: str  # 'URGENT' | 'WARNING'
    reasons: list[str]
    notif: Optional[NotifInline] = None


class WatchlistSummary(BaseModel):
    total: int
    urgent: int
    warning: int
    vip_at_risk: int
    notified: int
    not_notified: int


class WatchlistResponse(BaseModel):
    summary: WatchlistSummary
    visits: list[WatchlistVisit]


def _parse_eta(eta_raw) -> Optional[datetime]:
    """Parsea current_eta_cl a datetime. Acepta 'YYYY-MM-DD HH:MM:SS' o ISO."""
    if eta_raw is None:
        return None
    if isinstance(eta_raw, datetime):
        return eta_raw
    s = str(eta_raw).strip()
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s)
        return datetime.fromisoformat(s.replace(" ", "T"))
    except Exception:  # noqa: BLE001
        return None


def _now_for_watchlist() -> datetime:
    if STATE.sim_clock is not None:
        return STATE.sim_clock
    return datetime.utcnow()


def _compute_urgency(
    eta_dt: Optional[datetime],
    now: datetime,
    is_vip: bool,
    priority: str,
) -> tuple[Optional[str], float, list[str]]:
    """Devuelve (severity, score, reasons). severity=None significa "fuera del watchlist"."""
    reasons: list[str] = []
    if eta_dt is None:
        # Sin ETA no podemos juzgar urgencia: solo entra si es VIP o priority=high/vip
        if is_vip or priority in ("vip", "high"):
            reasons.append("ETA desconocido")
            sev = "WARNING"
            score = 35.0
            if is_vip:
                reasons.append("Cliente VIP")
                score += 10
            return sev, score, reasons
        return None, 0.0, reasons

    mins_to_eta = (eta_dt - now).total_seconds() / 60.0
    score = 0.0
    sev: Optional[str] = None

    if mins_to_eta <= 0:
        sev = "URGENT"
        score = 70.0 + min(30.0, abs(mins_to_eta) / 2.0)  # cap 100
        reasons.append(f"ETA vencida hace {abs(mins_to_eta):.0f} min")
    elif mins_to_eta <= 30:
        sev = "WARNING"
        score = 40.0 + (30.0 - mins_to_eta)  # más cerca, más score
        reasons.append(f"ETA en {mins_to_eta:.0f} min")
    else:
        # Fuera del watchlist a menos que sea VIP / priority high
        if is_vip:
            sev = "WARNING"
            score = 30.0
            reasons.append(f"VIP con ETA en {mins_to_eta:.0f} min")
        elif priority in ("vip", "high"):
            sev = "WARNING"
            score = 28.0
            reasons.append(f"Prioridad {priority} con ETA en {mins_to_eta:.0f} min")
        else:
            return None, 0.0, reasons

    if is_vip and "Cliente VIP" not in reasons and "VIP" not in " ".join(reasons):
        reasons.append("Cliente VIP")
        score += 10.0
    if priority == "vip" and "Prioridad" not in " ".join(reasons):
        reasons.append("Prioridad VIP")
        score += 8.0
    elif priority == "high":
        reasons.append("Prioridad alta")
        score += 5.0

    return sev, min(100.0, round(score, 1)), reasons


@router.get("", response_model=WatchlistResponse)
def get_watchlist(
    empresa_id: Optional[int] = Query(default=None),
    region: str = Query(default="all", pattern="^(all|RM|regiones)$"),
    only_vip: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> WatchlistResponse:
    today = STATE.today
    if today is None:
        return WatchlistResponse(
            summary=WatchlistSummary(total=0, urgent=0, warning=0, vip_at_risk=0, notified=0, not_notified=0),
            visits=[],
        )
    today_iso = today.isoformat()

    # Scope empresa
    scope_empresa: Optional[int] = None
    if not user.is_falabella:
        scope_empresa = user.empresa_id
    elif empresa_id is not None:
        scope_empresa = empresa_id

    # Query
    where = ["planned_date = ?", "status = 'pending'"]
    params: list = [today_iso]
    if scope_empresa is not None:
        where.append("empresa_falsa = ?")
        params.append(scope_empresa)
    if region == "RM":
        where.append("(region = 'RM' OR region IS NULL)")
    elif region == "regiones":
        where.append("region IS NOT NULL AND region <> 'RM'")

    sql = f"""
        SELECT id, title, reference, comuna, region, address,
               patente_falsa, empresa_falsa, driver_name, current_eta_cl
        FROM fpoc.simpli_visits
        WHERE {" AND ".join(where)}
    """

    empresas_cat = {int(e["empresa_id"]): e["nombre"] for e in STATE.empresas}
    driver_by_vid = {int(v["vehicle_id"]): v.get("driver_name") for v in STATE.vehicles_ext}

    rows: list[dict] = []
    titles: list[str] = []
    tids: list[str] = []
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(sql, *params)
        for r in cur.fetchall():
            tid = str(r.id)
            title = r.title or ""
            rows.append({
                "id": tid,
                "title": title,
                "reference": r.reference,
                "comuna": r.comuna,
                "region": (r.region or "regiones"),
                "address": r.address,
                "patente_falsa": int(r.patente_falsa) if r.patente_falsa is not None else None,
                "empresa_falsa": int(r.empresa_falsa) if r.empresa_falsa is not None else None,
                "driver_name": r.driver_name,
                "current_eta_cl": r.current_eta_cl,
            })
            titles.append(title)
            tids.append(tid)

    vip_meta: dict[str, dict] = {}  # title -> {tier, deadline_time}
    priority_map: dict[str, str] = {}
    notif_map: dict[str, dict] = {}

    if tids:
        with get_conn() as cn:
            cur = cn.cursor()
            unique_titles = list({t for t in titles if t})
            for i in range(0, len(unique_titles), 500):
                batch = unique_titles[i:i + 500]
                marks = ",".join(["?"] * len(batch))
                try:
                    cur.execute(
                        f"""
                        SELECT match_value, tier, deadline_time
                        FROM fpoc.vip_clients
                        WHERE active = 1 AND match_type = 'title' AND match_value IN ({marks})
                        """,
                        *batch,
                    )
                    for r in cur.fetchall():
                        vip_meta[r.match_value] = {
                            "tier": r.tier,
                            "deadline_time": str(r.deadline_time) if r.deadline_time else None,
                        }
                except Exception:  # noqa: BLE001
                    pass
            # Priority overrides
            for i in range(0, len(tids), 500):
                batch = tids[i:i + 500]
                marks = ",".join(["?"] * len(batch))
                try:
                    cur.execute(
                        f"SELECT tracking_id, priority FROM fpoc.visit_priority_overrides "
                        f"WHERE tracking_id IN ({marks})",
                        *batch,
                    )
                    for r in cur.fetchall():
                        priority_map[r.tracking_id] = r.priority
                except Exception:  # noqa: BLE001
                    pass
            # Last notification per tracking_id
            for i in range(0, len(tids), 500):
                batch = tids[i:i + 500]
                marks = ",".join(["?"] * len(batch))
                try:
                    cur.execute(
                        f"""
                        WITH ranked AS (
                          SELECT tracking_id, status, created_at,
                                 COUNT(*) OVER (PARTITION BY tracking_id) AS n,
                                 SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) OVER (PARTITION BY tracking_id) AS sent_n,
                                 ROW_NUMBER() OVER (PARTITION BY tracking_id ORDER BY created_at DESC) AS rn
                          FROM fpoc.notifications_log
                          WHERE tracking_id IN ({marks})
                        )
                        SELECT tracking_id, n, sent_n, status, created_at FROM ranked WHERE rn = 1
                        """,
                        *batch,
                    )
                    for r in cur.fetchall():
                        notif_map[r.tracking_id] = {
                            "count": int(r.n),
                            "sent_count": int(r.sent_n or 0),
                            "last_status": r.status,
                            "last_created_at": r.created_at.isoformat()
                                if hasattr(r.created_at, "isoformat") else str(r.created_at),
                        }
                except Exception:  # noqa: BLE001
                    pass

    now = _now_for_watchlist()
    visits: list[WatchlistVisit] = []
    urgent = warning = vip_at_risk = notified = 0

    for row in rows:
        tid = row["id"]
        title = row["title"]
        vip_info = vip_meta.get(title)
        is_vip = vip_info is not None

        if only_vip and not is_vip:
            continue

        prio = priority_map.get(tid, "vip" if is_vip else "normal")
        eta_dt = _parse_eta(row["current_eta_cl"])
        sev, score, reasons = _compute_urgency(eta_dt, now, is_vip, prio)
        if sev is None:
            continue

        if sev == "URGENT":
            urgent += 1
        else:
            warning += 1
        if is_vip:
            vip_at_risk += 1

        nm = notif_map.get(tid)
        if nm:
            notified += 1

        empresa_id_val = row["empresa_falsa"]
        empresa_nombre = empresas_cat.get(empresa_id_val) if empresa_id_val is not None else None
        vehicle_id = row["patente_falsa"]
        eta_str: Optional[str] = None
        if eta_dt is not None:
            eta_str = eta_dt.strftime("%H:%M")
        elif row["current_eta_cl"]:
            eta_str = str(row["current_eta_cl"])[:5]

        status_label = "ETA VENCIDA" if sev == "URGENT" else "EN RIESGO"

        visits.append(WatchlistVisit(
            tracking_id=tid,
            vehicle_id=vehicle_id,
            vehicle_name=f"PAT-{vehicle_id}" if vehicle_id is not None else None,
            driver_name=row.get("driver_name") or (
                driver_by_vid.get(int(vehicle_id)) if vehicle_id is not None else None
            ),
            empresa_id=empresa_id_val,
            empresa_nombre=empresa_nombre,
            title=title,
            address=row.get("address"),
            comuna=row.get("comuna"),
            region=row.get("region") or "regiones",
            estimated_time_arrival=eta_str,
            status_label=status_label,
            is_vip=is_vip,
            vip_tier=(vip_info or {}).get("tier"),
            vip_deadline_time=(vip_info or {}).get("deadline_time"),
            priority=prio,
            urgency_score=score,
            severity=sev,
            reasons=reasons,
            notif=NotifInline(**nm) if nm else None,
        ))

    visits.sort(key=lambda v: v.urgency_score, reverse=True)

    return WatchlistResponse(
        summary=WatchlistSummary(
            total=len(visits),
            urgent=urgent,
            warning=warning,
            vip_at_risk=vip_at_risk,
            notified=notified,
            not_notified=len(visits) - notified,
        ),
        visits=visits,
    )

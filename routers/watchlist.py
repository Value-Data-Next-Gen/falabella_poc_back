"""Watchlist: visitas pending scoreadas por urgencia.

Objetivo: en 3s el operador ve quién va a fallar y puede actuar.

Score de urgencia (0-100):
  p_fallo       base 0-40
  slack_min<0   +30    (ya pasó el deadline)
  slack_min<30  +20
  slack_min<60  +10
  alert_slack RED  +15
  VIP              +15
  priority=high    +10
  priority=vip     +20 (reemplaza anterior si aplica)

Severity:
  >= 70  CRITICO
  >= 45  ALTO
  >= 25  MEDIO
  < 25   (se excluye del watchlist)

Scope: transport_manager ve solo vehículos de su empresa.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from routers.comments import _visit_region
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
    vehicle_id: int
    vehicle_name: Optional[str] = None
    driver_name: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    title: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    order: int
    window_end: str
    estimated_time_arrival: str
    slack_min: float
    alert_slack: str
    p_fallo: float
    alert_valuedata: bool
    is_vip: bool
    vip_tier: Optional[str] = None
    vip_deadline_time: Optional[str] = None
    priority: str
    urgency_score: float
    severity: str  # 'CRITICO' | 'ALTO' | 'MEDIO'
    reasons: list[str]
    region: str
    notif: Optional[NotifInline] = None


class WatchlistSummary(BaseModel):
    total: int
    critico: int
    alto: int
    medio: int
    vip_at_risk: int
    notified: int
    not_notified: int


class WatchlistResponse(BaseModel):
    summary: WatchlistSummary
    visits: list[WatchlistVisit]


def _score_and_reasons(row, is_vip: bool, priority: str) -> tuple[float, str, list[str]]:
    score = 0.0
    reasons: list[str] = []

    pf = float(row["p_fallo"])
    score += pf * 40
    if pf >= 0.7:
        reasons.append(f"P(fallo) {pf*100:.0f}%")
    elif pf >= 0.4:
        reasons.append(f"Riesgo medio {pf*100:.0f}%")

    slack = float(row["slack_min"])
    if slack < 0:
        score += 30
        reasons.append(f"Slack negativo {slack:.0f}min")
    elif slack < 30:
        score += 20
        reasons.append(f"Slack crítico {slack:.0f}min")
    elif slack < 60:
        score += 10

    alert_slack = str(row.get("alert_slack", ""))
    if alert_slack == "RED":
        score += 15
        reasons.append("Rojo SimpliRoute")

    if bool(row.get("alert_valuedata", False)):
        reasons.append("Alerta VD activa")

    if is_vip:
        score += 15
        reasons.append("Cliente VIP")

    if priority == "vip":
        score = max(score, score + 20)
        if "Cliente VIP" not in reasons:
            reasons.append("Prioridad VIP")
    elif priority == "high":
        score += 10
        reasons.append("Prioridad alta")

    score = min(100, score)
    sev = "CRITICO" if score >= 70 else ("ALTO" if score >= 45 else "MEDIO")
    return score, sev, reasons


@router.get("", response_model=WatchlistResponse)
def get_watchlist(
    empresa_id: Optional[int] = Query(default=None),
    region: str = Query(default="all", pattern="^(all|RM|regiones)$"),
    only_vip: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> WatchlistResponse:
    if STATE.snapshot_df is None:
        return WatchlistResponse(
            summary=WatchlistSummary(total=0, critico=0, alto=0, medio=0, vip_at_risk=0, notified=0, not_notified=0),
            visits=[],
        )

    df = STATE.snapshot_df.copy()
    df["empresa_id"] = df["vehicle_id"].astype(int).map(STATE.vehicle_empresa_map)

    if not user.is_falabella:
        df = df[df["empresa_id"] == user.empresa_id]
    elif empresa_id is not None:
        df = df[df["empresa_id"] == empresa_id]

    # Solo pending
    df = df[df["status"] == "pending"]

    # Region filter
    df["region"] = df.apply(lambda r: _visit_region(r.get("latitude"), r.get("longitude")), axis=1)
    if region != "all":
        df = df[df["region"] == region]

    # Lookup de VIP titles + priority overrides
    tids = df["tracking_id"].astype(str).unique().tolist()
    titles = df["title"].astype(str).unique().tolist()
    vip_meta: dict[str, dict] = {}  # title -> {tier, deadline_time}
    priority_map: dict[str, str] = {}
    notif_map: dict[str, dict] = {}
    empresas_cat = {int(e["empresa_id"]): e["nombre"] for e in STATE.empresas}
    driver_by_vid = {int(v["vehicle_id"]): v["driver_name"] for v in STATE.vehicles_ext}

    if tids:
        with get_conn() as cn:
            cur = cn.cursor()
            # VIP by title (batched, with metadata)
            for i in range(0, len(titles), 500):
                batch = titles[i:i + 500]
                marks = ",".join(["?"] * len(batch))
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
            # Priority overrides
            for i in range(0, len(tids), 500):
                batch = tids[i:i + 500]
                marks = ",".join(["?"] * len(batch))
                cur.execute(
                    f"SELECT tracking_id, priority FROM fpoc.visit_priority_overrides "
                    f"WHERE tracking_id IN ({marks})",
                    *batch,
                )
                for r in cur.fetchall():
                    priority_map[r.tracking_id] = r.priority
            # Last notification per tracking_id
            for i in range(0, len(tids), 500):
                batch = tids[i:i + 500]
                marks = ",".join(["?"] * len(batch))
                cur.execute(
                    f"""
                    WITH ranked AS (
                      SELECT tracking_id, status, created_at, COUNT(*) OVER (PARTITION BY tracking_id) AS n,
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
                        "last_created_at": r.created_at.isoformat(),
                    }

    visits: list[WatchlistVisit] = []
    critico = alto = medio = vip_at_risk = notified = 0

    for _, row in df.iterrows():
        tid = str(row["tracking_id"])
        title = str(row["title"])
        vip_info = vip_meta.get(title)
        is_vip = vip_info is not None

        # Filtro only_vip
        if only_vip and not is_vip:
            continue

        prio = priority_map.get(tid, "vip" if is_vip else "normal")
        score, sev, reasons = _score_and_reasons(row, is_vip, prio)
        if score < 25:
            continue

        if sev == "CRITICO": critico += 1
        elif sev == "ALTO": alto += 1
        else: medio += 1
        if is_vip: vip_at_risk += 1

        nm = notif_map.get(tid)
        if nm: notified += 1

        eid = int(row["empresa_id"]) if row.get("empresa_id") is not None else None
        visits.append(WatchlistVisit(
            tracking_id=tid,
            vehicle_id=int(row["vehicle_id"]),
            vehicle_name=str(row["vehicle_name"]),
            driver_name=driver_by_vid.get(int(row["vehicle_id"]), f"Driver {row['vehicle_id']}"),
            empresa_id=eid,
            empresa_nombre=empresas_cat.get(eid) if eid is not None else None,
            title=title,
            address=str(row["address"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            order=int(row["order"]),
            window_end=str(row["window_end"]),
            estimated_time_arrival=str(row["estimated_time_arrival"]),
            slack_min=float(row["slack_min"]),
            alert_slack=str(row["alert_slack"]),
            p_fallo=float(row["p_fallo"]),
            alert_valuedata=bool(row["alert_valuedata"]),
            is_vip=is_vip,
            vip_tier=(vip_info or {}).get("tier"),
            vip_deadline_time=(vip_info or {}).get("deadline_time"),
            priority=prio,
            urgency_score=round(score, 1),
            severity=sev,
            reasons=reasons,
            region=str(row.get("region", "regiones")),
            notif=NotifInline(**nm) if nm else None,
        ))

    # Ordenar por urgencia desc
    visits.sort(key=lambda v: v.urgency_score, reverse=True)

    return WatchlistResponse(
        summary=WatchlistSummary(
            total=len(visits),
            critico=critico, alto=alto, medio=medio,
            vip_at_risk=vip_at_risk,
            notified=notified,
            not_notified=len(visits) - notified,
        ),
        visits=visits,
    )

"""Plan diario: vista jerárquica Empresa → Ruta → Visitas ordenadas.

Sprint 6: la respuesta default lee del dataset REAL `fpoc_simpli_visits`.
El cliente Falabella reportó que veía datos sintéticos del pipeline ML (~120
visitas/día) en vez del dataset real (~350+/día). Plan operativo ahora usa la
data real; el modelo XGB sigue entrenando con sintéticos pero solo se aplica a
las vistas analíticas (Watchlist, ModelPanel).

Sprint 2: la respuesta agrupa por Empresa → Rutas → Visitas con orden y
progreso. Sprint 6 cambia la entidad ruta a `ruta_id` (R-YYYYMMDD-NNN).

Compat: ?legacy=true devuelve la forma vieja (Empresa → Drivers vía snapshot
sintético). ?source=synthetic mantiene el comportamiento previo (snapshot_df).

Filtros:
  empresa_id   : admin/ops puede filtrar (transport_manager fixed a su empresa)
  region       : 'all' | 'RM' | 'regiones' (lee columna `region` del dataset)
  only_vip     : true => solo visitas que matchean fpoc_vip_clients
  planned_date : opcional 'YYYY-MM-DD' (default: STATE.today, o MAX si no existe)
  source       : 'real' (default) | 'synthetic' (snapshot_df)

Salida (estructura nueva Sprint 6):
  empresas[].rutas[] => { ruta_id, patente, driver_name, region, ct, ... }
  visitas[]          => { tracking_id, folio, current_eta_cl, region, comuna, ... }
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as date_cls, datetime
from typing import Optional

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import CurrentUser, current_user
from comments import _visit_region
from db import get_conn
from state import STATE

router = APIRouter(prefix="/api/plan-diario", tags=["plan-diario"])


# =============================================================================
# Schemas — nueva estructura (Sprint 6: ruta_id como string)
# =============================================================================
class PlanVisit(BaseModel):
    tracking_id: str
    order: int
    title: str
    cliente_nombre: str          # alias de title
    address: str
    comuna: Optional[str] = None
    region: str                  # 'RM' | 'Valparaíso' | ...
    latitude: float = 0.0
    longitude: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    window_start: str = ""
    window_end: str = ""
    planned_arrival_time: str = ""
    estimated_time_arrival: str = ""
    current_eta_cl: str = ""     # NEW Sprint 6: ETA dataset real (HH:MM-aware)
    slack_min: float = 0.0
    alert_slack: str = "GREEN"
    p_fallo: float = 0.0
    status: str
    priority: str = "normal"     # 'low' | 'normal' | 'high' | 'vip'
    priority_reason: Optional[str] = None
    is_vip: bool = False
    vip_tier: Optional[str] = None
    vip_deadline_time: Optional[str] = None
    alert_valuedata: bool = False
    folio: Optional[str] = None  # NEW Sprint 6: reference (siempre, destacado si VIP)
    motivo_reportado: Optional[str] = None
    severity: Optional[str] = None


class PlanRuta(BaseModel):
    ruta_id: str                 # NEW Sprint 6: 'R-YYYYMMDD-NNN'
    vehicle_id: int              # = Patente_falsa (compat)
    vehicle_name: str            # 'FAL-XXXX' (synth) o 'PAT-NNN' (real)
    plate: Optional[str] = None
    patente: Optional[str] = None  # NEW alias
    driver_name: str
    region: str = "RM"           # NEW Sprint 6: region dominante de la ruta
    ct: Optional[str] = None     # NEW Sprint 6: centro de despacho
    next_stop_order: Optional[int] = None  # NEW alias de orden_actual
    orden_actual: Optional[int] = None     # próximo stop pendiente
    total_visitas: int
    completadas: int
    pendientes: int
    fallidas: int
    en_riesgo: int
    progreso_pct: float
    red_visitas: int = 0
    vip_visitas: int = 0
    high_priority: int = 0
    visitas: list[PlanVisit]


class PlanEmpresaNew(BaseModel):
    empresa_id: int
    empresa_nombre: str
    total_visitas: int
    completadas: int
    pendientes: int
    fallidas: int
    en_riesgo: int
    red_visitas: int = 0
    vip_visitas: int = 0
    high_priority: int = 0
    rutas: list[PlanRuta]


class PlanDiarioResponseNew(BaseModel):
    planned_date: str
    sim_clock: str
    region: str
    only_vip: bool
    source: str                  # 'real' | 'synthetic'
    empresas: list[PlanEmpresaNew]


# =============================================================================
# Schemas — legacy (Sprint 1, mantener para compat)
# =============================================================================
class PlanVisitLegacy(BaseModel):
    tracking_id: str
    order: int
    title: str
    address: str
    latitude: float
    longitude: float
    window_start: str
    window_end: str
    planned_arrival_time: str
    estimated_time_arrival: str
    slack_min: float
    alert_slack: str
    p_fallo: float
    status: str
    priority: str
    priority_reason: Optional[str] = None
    is_vip: bool
    alert_valuedata: bool


class PlanDriverLegacy(BaseModel):
    vehicle_id: int
    vehicle_name: str
    driver_name: str
    total_visits: int
    completed: int
    pending: int
    red_visits: int
    vip_visits: int
    high_priority: int
    visits: list[PlanVisitLegacy]


class PlanEmpresaLegacy(BaseModel):
    empresa_id: int
    nombre: str
    total_visits: int
    completed: int
    pending: int
    red_visits: int
    vip_visits: int
    high_priority: int
    drivers: list[PlanDriverLegacy]


class PlanDiarioResponseLegacy(BaseModel):
    planned_date: str
    sim_clock: str
    empresas: list[PlanEmpresaLegacy]


# =============================================================================
# Helpers comunes
# =============================================================================
def _load_vip_match(titles: list[str]) -> dict[str, dict]:
    """title -> { tier, deadline_time, alert_minutes_before }."""
    out: dict[str, dict] = {}
    if not titles:
        return out
    with get_conn() as cn:
        cur = cn.cursor()
        for i in range(0, len(titles), 500):
            batch = titles[i:i + 500]
            marks = ",".join(["?"] * len(batch))
            cur.execute(
                f"""
                SELECT match_value, tier, deadline_time, alert_minutes_before
                FROM fpoc.vip_clients
                WHERE active = 1 AND match_type = 'title' AND match_value IN ({marks})
                """,
                *batch,
            )
            for r in cur.fetchall():
                out[r.match_value] = {
                    "tier": r.tier,
                    "deadline_time": str(r.deadline_time) if r.deadline_time else None,
                    "alert_minutes_before": int(r.alert_minutes_before) if r.alert_minutes_before is not None else 60,
                }
    return out


def _load_priority_overrides(tids: list[str]) -> dict[str, tuple[str, Optional[str]]]:
    out: dict[str, tuple[str, Optional[str]]] = {}
    if not tids:
        return out
    with get_conn() as cn:
        cur = cn.cursor()
        for i in range(0, len(tids), 500):
            batch = tids[i:i + 500]
            marks = ",".join(["?"] * len(batch))
            cur.execute(
                f"SELECT tracking_id, priority, reason FROM fpoc.visit_priority_overrides "
                f"WHERE tracking_id IN ({marks})",
                *batch,
            )
            for r in cur.fetchall():
                out[r.tracking_id] = (r.priority, r.reason)
    return out


def _load_last_motivo(tids: list[str]) -> dict[str, tuple[str, str]]:
    """tracking_id -> (motivo, severity_resolved). Best-effort."""
    out: dict[str, tuple[str, str]] = {}
    if not tids:
        return out
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            for i in range(0, len(tids), 500):
                batch = tids[i:i + 500]
                marks = ",".join(["?"] * len(batch))
                cur.execute(
                    f"""
                    SELECT c.tracking_id, c.motivo, c.empresa_id
                    FROM fpoc_visit_comments c
                    INNER JOIN (
                      SELECT tracking_id, MAX(comment_id) AS max_id
                      FROM fpoc_visit_comments
                      WHERE tracking_id IN ({marks})
                      GROUP BY tracking_id
                    ) t ON t.tracking_id = c.tracking_id AND t.max_id = c.comment_id
                    """,
                    *batch,
                )
                from comments import _resolve_alert_config
                for r in cur.fetchall():
                    eid = int(r.empresa_id) if r.empresa_id is not None else None
                    _alertable, sev = _resolve_alert_config(r.motivo, eid)
                    out[r.tracking_id] = (r.motivo, sev)
    except Exception:  # noqa: BLE001
        pass
    return out


def _resolve_planned_date(requested: Optional[str]) -> str:
    """Devuelve planned_date a usar: el solicitado, STATE.today si existe en
    la DB, o MAX(planned_date) como fallback."""
    if requested:
        return requested
    today_iso = STATE.today.isoformat() if STATE.today else None
    with get_conn() as cn:
        cur = cn.cursor()
        if today_iso:
            cur.execute(
                "SELECT 1 FROM fpoc_simpli_visits WHERE planned_date = ?",
                today_iso,
            )
            if cur.fetchone():
                return today_iso
        cur.execute("SELECT MAX(planned_date) FROM fpoc_simpli_visits")
        r = cur.fetchone()
        if r and r[0]:
            return str(r[0])[:10]
    # Fallback final
    return today_iso or date_cls.today().isoformat()


def _compute_p_fallo_from_sla(sla_hour: float, status: str, ruta_anomala: int) -> float:
    """Heurística determinista basada en `sla_hour_checkout_eta` (negativo =
    entregado tarde). Para visitas pendientes se infiere riesgo del SLA
    proyectado.
    Retorna [0..1].
    """
    if status == "failed":
        # ya falló — no es "p_fallo proyectado", pero lo marcamos alto para
        # que la UI lo destaque.
        return 0.95
    if status == "completed":
        # ya entregada (success) — riesgo cero. Lo dejamos en función del
        # retraso para mostrar histórico.
        if sla_hour is None:
            return 0.0
        if sla_hour < -2:
            return 0.65
        if sla_hour < 0:
            return 0.35
        return 0.05
    # pending / otro
    base = 0.10
    if sla_hour is not None:
        if sla_hour < -3:
            base = 0.85
        elif sla_hour < -1:
            base = 0.65
        elif sla_hour < 0:
            base = 0.45
        elif sla_hour < 1:
            base = 0.25
        else:
            base = 0.10
    if ruta_anomala:
        base = min(0.95, base + 0.15)
    return round(base, 3)


def _slack_from_sla(sla_hour: float) -> tuple[float, str]:
    """slack en min + alert_slack tag."""
    if sla_hour is None:
        return 0.0, "GREEN"
    slack = float(sla_hour) * 60.0
    if slack < 0:
        return slack, "RED"
    if slack < 30:
        return slack, "YELLOW"
    return slack, "GREEN"


def _format_eta_hhmm(eta_str: str) -> str:
    """Toma '2026-04-19 13:53:00' -> '13:53'. Maneja sufijo UTC y formatos varios."""
    if not eta_str:
        return ""
    try:
        s = str(eta_str).strip()
        # Quitar sufijo "UTC" / fracciones
        s = s.replace(" UTC", "").replace(" CL", "")
        if "T" in s:
            s = s.replace("T", " ")
        # 'YYYY-MM-DD HH:MM:SS[.fff]' o 'YYYY-MM-DD HH:MM'
        if " " in s:
            tpart = s.split(" ", 1)[1]
        else:
            tpart = s
        return tpart[:5]  # HH:MM
    except Exception:  # noqa: BLE001
        return str(eta_str)[:5]


def _is_failed(row) -> bool:
    return row.get("status", "") == "failed"


def _is_at_risk(row) -> bool:
    if row.get("status", "") != "pending":
        # Para dataset real, "en riesgo" = no completada Y con sla negativo
        if row.get("status") == "failed":
            return True
        return False
    p = float(row.get("p_fallo", 0))
    if p >= 0.5:
        return True
    if str(row.get("alert_slack", "")) == "RED":
        return True
    return False


# =============================================================================
# Endpoint principal
# =============================================================================
@router.get("")
def get_plan_diario(
    empresa_id: Optional[int] = Query(default=None),
    region: str = Query(default="all"),
    only_vip: bool = Query(default=False),
    legacy: bool = Query(default=False),
    source: str = Query(default="real", pattern="^(real|synthetic)$"),
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
):
    # Scope
    if not user.is_falabella:
        empresa_id = user.empresa_id

    # Modo legacy (Sprint 1) — siempre lee snapshot sintético
    if legacy:
        return _build_legacy_from_snapshot(empresa_id, region)

    # Source: real (default Sprint 6) o synthetic (compat)
    if source == "synthetic":
        return _build_new_from_snapshot(empresa_id, region, only_vip)

    return _build_new_from_real(empresa_id, region, only_vip, planned_date)


# =============================================================================
# Builder: dataset REAL (fpoc_simpli_visits) — Sprint 6
# =============================================================================
def _build_new_from_real(
    empresa_id: Optional[int],
    region: str,
    only_vip: bool,
    planned_date_q: Optional[str],
) -> PlanDiarioResponseNew:
    """Lee fpoc_simpli_visits, agrupa por empresa → ruta_id, devuelve estructura nueva."""
    pd_iso = _resolve_planned_date(planned_date_q)

    # Empresas catalog
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1")
        empresas_catalog = {int(r.empresa_id): r.nombre for r in cur.fetchall()}

        # WHERE clauses
        where = ["planned_date = ?"]
        params: list = [pd_iso]
        if empresa_id is not None:
            where.append("Empresa_falsa = ?")
            params.append(int(empresa_id))
        # region: 'all' | 'RM' | 'regiones' | 'Valparaíso' | ...
        # Compat con UI: 'regiones' = todas excepto RM
        if region == "RM":
            where.append("(region = 'RM' OR region IS NULL)")  # NULL → asume RM (default)
        elif region == "regiones":
            where.append("region IS NOT NULL AND region != 'RM'")
        elif region not in ("all", ""):
            where.append("region = ?")
            params.append(region)

        where_sql = " AND ".join(where)
        sql = f"""
            SELECT id, planned_date, title, "order", address, comuna, region, ruta_id,
                   ct, status, current_eta_cl, sla_hour_checkout_eta,
                   bin_label, ruta_anomala, reference,
                   Empresa_falsa, Patente_falsa, Drivername, Fechainicioruta_hora_cl
            FROM fpoc_simpli_visits
            WHERE {where_sql}
        """
        cur.execute(sql, *params)
        rows = cur.fetchall()

    if not rows:
        return PlanDiarioResponseNew(
            planned_date=pd_iso,
            sim_clock=(STATE.sim_clock.isoformat() if STATE.sim_clock else f"{pd_iso}T09:00:00"),
            region=region,
            only_vip=only_vip,
            source="real",
            empresas=[],
        )

    # Construir dicts de visita
    visit_rows: list[dict] = []
    titles: list[str] = []
    tids: list[str] = []
    for r in rows:
        tid = str(r.id)
        title = str(r.title) if r.title else ""
        sla_h = float(r.sla_hour_checkout_eta) if r.sla_hour_checkout_eta is not None else 0.0
        slack_min, alert_slack = _slack_from_sla(sla_h)
        p_fallo = _compute_p_fallo_from_sla(sla_h, r.status, int(r.ruta_anomala or 0))
        d = {
            "id": tid,
            "tracking_id": tid,
            "order": int(r.__getattr__("order")) if hasattr(r, "order") else int(r["order"]),
            "title": title,
            "address": str(r.address) if r.address else "",
            "comuna": str(r.comuna) if r.comuna else None,
            "region": str(r.region) if r.region else "RM",
            "ct": str(r.ct) if r.ct else None,
            "status": str(r.status) if r.status else "pending",
            "current_eta_cl": str(r.current_eta_cl) if r.current_eta_cl else "",
            "current_eta_hhmm": _format_eta_hhmm(str(r.current_eta_cl)) if r.current_eta_cl else "",
            "sla_hour": sla_h,
            "slack_min": slack_min,
            "alert_slack": alert_slack,
            "p_fallo": p_fallo,
            "ruta_anomala": int(r.ruta_anomala or 0),
            "ruta_id": str(r.ruta_id) if r.ruta_id else "",
            "empresa_id": int(r.Empresa_falsa),
            "patente": int(r.Patente_falsa),
            "drivername": str(r.Drivername) if r.Drivername else "",
            "fecha_inicio_hora": str(r.Fechainicioruta_hora_cl) if r.Fechainicioruta_hora_cl else "",
            "reference": int(r.reference) if r.reference is not None else 0,
            "alert_valuedata": (alert_slack == "RED" and r.status == "pending"),
        }
        visit_rows.append(d)
        titles.append(title)
        tids.append(tid)

    # VIP / priority / motivo
    vip_map = _load_vip_match(list(set(titles)))
    priority_map = _load_priority_overrides(tids)
    motivo_map = _load_last_motivo(tids)

    # Filtro only_vip
    if only_vip:
        visit_rows = [v for v in visit_rows if v["title"] in vip_map]

    # Agrupar por empresa → ruta_id
    by_empresa: dict[int, dict] = defaultdict(lambda: {"rutas": defaultdict(list)})
    for v in visit_rows:
        eid = v["empresa_id"]
        rid = v["ruta_id"] or f"R-{pd_iso.replace('-', '')}-?"
        by_empresa[eid]["rutas"][rid].append(v)

    out_empresas: list[PlanEmpresaNew] = []
    for eid in sorted(by_empresa.keys()):
        rutas_dict = by_empresa[eid]["rutas"]
        rutas_out: list[PlanRuta] = []
        e_total = e_comp = e_pend = e_failed = e_risk = 0
        e_red = e_vip = e_high = 0

        for rid in sorted(rutas_dict.keys()):
            ruta_visits = sorted(rutas_dict[rid], key=lambda x: x["order"])
            visitas_out: list[PlanVisit] = []
            r_red = r_vip_count = r_high = r_failed = r_risk = 0
            ct_set = set()
            region_set = set()

            for vd in ruta_visits:
                tid = vd["tracking_id"]
                title = vd["title"]
                prio, reason = priority_map.get(tid, ("normal", None))
                vip_info = vip_map.get(title)
                is_vip = vip_info is not None
                vip_tier = vip_info["tier"] if is_vip else None
                vip_deadline = vip_info["deadline_time"] if is_vip else None
                if is_vip and prio in ("normal", "low"):
                    prio = "vip"
                if prio == "vip":
                    r_vip_count += 1
                if prio == "high":
                    r_high += 1
                if vd["alert_slack"] == "RED" and vd["status"] == "pending":
                    r_red += 1
                if _is_failed(vd):
                    r_failed += 1
                if _is_at_risk(vd):
                    r_risk += 1
                motivo, sev = motivo_map.get(tid, (None, None))
                ct_set.add(vd["ct"] or "")
                region_set.add(vd["region"] or "RM")

                folio = f"#{vd['reference']}" if vd.get("reference") else None

                visitas_out.append(PlanVisit(
                    tracking_id=tid,
                    order=vd["order"],
                    title=title,
                    cliente_nombre=title,
                    address=vd["address"],
                    comuna=vd["comuna"],
                    region=vd["region"],
                    estimated_time_arrival=vd["current_eta_hhmm"],
                    current_eta_cl=vd["current_eta_cl"],
                    slack_min=vd["slack_min"],
                    alert_slack=vd["alert_slack"],
                    p_fallo=vd["p_fallo"],
                    status=vd["status"],
                    priority=prio,
                    priority_reason=reason,
                    is_vip=is_vip,
                    vip_tier=vip_tier,
                    vip_deadline_time=vip_deadline,
                    alert_valuedata=vd["alert_valuedata"],
                    folio=folio,
                    motivo_reportado=motivo,
                    severity=sev,
                ))

            total = len(ruta_visits)
            comp = sum(1 for v in ruta_visits if v["status"] == "completed")
            failed = sum(1 for v in ruta_visits if v["status"] == "failed")
            pend = total - comp - failed
            pending_visits = [v for v in visitas_out if v.status == "pending"]
            orden_actual = pending_visits[0].order if pending_visits else None
            progreso = ((comp + failed) / total * 100.0) if total else 0.0

            # Datos de la ruta (consistentes en todas las visitas)
            sample = ruta_visits[0]
            patente_str = f"PAT-{sample['patente']:03d}"
            ct_dom = next(iter(ct_set - {""}), None) or sample["ct"]
            region_dom = next(iter(region_set - {""}), "RM") or "RM"

            rutas_out.append(PlanRuta(
                ruta_id=rid,
                vehicle_id=sample["patente"],
                vehicle_name=patente_str,
                plate=patente_str,
                patente=patente_str,
                driver_name=sample["drivername"] or f"Driver {sample['patente']}",
                region=region_dom,
                ct=ct_dom,
                next_stop_order=orden_actual,
                orden_actual=orden_actual,
                total_visitas=total,
                completadas=comp,
                pendientes=pend,
                fallidas=failed,
                en_riesgo=r_risk,
                progreso_pct=round(progreso, 1),
                red_visitas=r_red,
                vip_visitas=r_vip_count,
                high_priority=r_high,
                visitas=visitas_out,
            ))
            e_total += total
            e_comp += comp
            e_pend += pend
            e_failed += failed
            e_risk += r_risk
            e_red += r_red
            e_vip += r_vip_count
            e_high += r_high

        out_empresas.append(PlanEmpresaNew(
            empresa_id=eid,
            empresa_nombre=empresas_catalog.get(eid, f"Empresa {eid}"),
            total_visitas=e_total,
            completadas=e_comp,
            pendientes=e_pend,
            fallidas=e_failed,
            en_riesgo=e_risk,
            red_visitas=e_red,
            vip_visitas=e_vip,
            high_priority=e_high,
            rutas=rutas_out,
        ))

    sim_clock_iso = STATE.sim_clock.isoformat() if STATE.sim_clock else f"{pd_iso}T09:00:00"
    return PlanDiarioResponseNew(
        planned_date=pd_iso,
        sim_clock=sim_clock_iso,
        region=region,
        only_vip=only_vip,
        source="real",
        empresas=out_empresas,
    )


# =============================================================================
# Builder: snapshot sintético (compat ?source=synthetic)
# =============================================================================
def _build_new_from_snapshot(
    empresa_id: Optional[int],
    region: str,
    only_vip: bool,
) -> PlanDiarioResponseNew:
    if STATE.snapshot_df is None or STATE.today is None or STATE.sim_clock is None:
        raise HTTPException(503, "Backend warming up")

    df = STATE.snapshot_df.copy()
    df["empresa_id"] = df["vehicle_id"].astype(int).map(STATE.vehicle_empresa_map)
    if empresa_id is not None:
        df = df[df["empresa_id"] == empresa_id]

    df["region"] = df.apply(lambda r: _visit_region(r.get("latitude"), r.get("longitude")), axis=1)
    if region == "RM":
        df = df[df["region"] == "RM"]
    elif region == "regiones":
        df = df[df["region"] != "RM"]
    elif region not in ("all", ""):
        df = df[df["region"] == region]

    driver_by_vid = {int(v["vehicle_id"]): v["driver_name"] for v in STATE.vehicles_ext}
    plate_by_vid = {int(v["vehicle_id"]): v.get("plate") for v in STATE.vehicles_ext}

    tids = df["tracking_id"].astype(str).unique().tolist()
    titles = df["title"].astype(str).unique().tolist()

    priority_map = _load_priority_overrides(tids)
    vip_map = _load_vip_match(titles)
    motivo_map = _load_last_motivo(tids)

    if only_vip:
        df = df[df["title"].astype(str).isin(set(vip_map.keys()))]

    empresas_catalog = {int(e["empresa_id"]): e["nombre"] for e in STATE.empresas}

    out_empresas: list[PlanEmpresaNew] = []
    today_iso = STATE.today.isoformat()
    today_compact = today_iso.replace("-", "")

    for eid, edf in df.groupby("empresa_id"):
        rutas_out: list[PlanRuta] = []
        e_total = e_comp = e_pend = e_failed = e_risk = 0
        e_red = e_vip = e_high = 0

        ruta_idx = 0
        for vid, vdf in edf.groupby("vehicle_id"):
            ruta_idx += 1
            ruta_id = f"R-{today_compact}-{ruta_idx:03d}"
            vdf_sorted = vdf.sort_values("order")
            visitas_out: list[PlanVisit] = []
            r_red = r_vip_count = r_high = r_failed = r_risk = 0
            for _, row in vdf_sorted.iterrows():
                tid = str(row["tracking_id"])
                title = str(row["title"])
                prio, reason = priority_map.get(tid, ("normal", None))
                vip_info = vip_map.get(title)
                is_vip = vip_info is not None
                vip_tier = vip_info["tier"] if is_vip else None
                vip_deadline = vip_info["deadline_time"] if is_vip else None
                if is_vip and prio in ("normal", "low"):
                    prio = "vip"
                if prio == "vip":
                    r_vip_count += 1
                if prio == "high":
                    r_high += 1
                if str(row.get("alert_slack", "")) == "RED" and row["status"] == "pending":
                    r_red += 1
                # synth: failed = status completed con failed=1
                synth_failed = (row.get("status") == "completed" and bool(row.get("failed", 0)))
                if synth_failed:
                    r_failed += 1
                if _is_at_risk_synth(row):
                    r_risk += 1
                motivo, sev = motivo_map.get(tid, (None, None))
                lat = float(row["latitude"])
                lon = float(row["longitude"])
                eta = str(row.get("estimated_time_arrival", ""))
                visitas_out.append(PlanVisit(
                    tracking_id=tid,
                    order=int(row["order"]),
                    title=title,
                    cliente_nombre=title,
                    address=str(row.get("address", "")),
                    comuna=str(row.get("comuna_id") or row.get("comuna") or "") or None,
                    region="RM" if _visit_region(lat, lon) == "RM" else "regiones",
                    latitude=lat, longitude=lon, lat=lat, lon=lon,
                    window_start=str(row.get("window_start", "")),
                    window_end=str(row.get("window_end", "")),
                    planned_arrival_time=str(row.get("planned_arrival_time", "")),
                    estimated_time_arrival=_format_eta_hhmm(eta),
                    current_eta_cl=eta,
                    slack_min=float(row.get("slack_min", 0.0)),
                    alert_slack=str(row.get("alert_slack", "GREEN")),
                    p_fallo=float(row.get("p_fallo", 0.0)),
                    status=str(row["status"]),
                    priority=prio,
                    priority_reason=reason,
                    is_vip=is_vip,
                    vip_tier=vip_tier,
                    vip_deadline_time=vip_deadline,
                    alert_valuedata=bool(row.get("alert_valuedata", False)),
                    folio=None,
                    motivo_reportado=motivo,
                    severity=sev,
                ))
            total = len(vdf_sorted)
            comp = int((vdf_sorted["status"] == "completed").sum())
            pend = total - comp
            pending_visits = [v for v in visitas_out if v.status == "pending"]
            orden_actual = pending_visits[0].order if pending_visits else None
            progreso = (comp / total * 100.0) if total else 0.0

            rutas_out.append(PlanRuta(
                ruta_id=ruta_id,
                vehicle_id=int(vid),
                vehicle_name=str(vdf_sorted["vehicle_name"].iloc[0]),
                plate=plate_by_vid.get(int(vid)),
                patente=plate_by_vid.get(int(vid)),
                driver_name=driver_by_vid.get(int(vid), f"Driver {vid}"),
                region="RM",
                ct=None,
                next_stop_order=orden_actual,
                orden_actual=orden_actual,
                total_visitas=total,
                completadas=comp,
                pendientes=pend,
                fallidas=r_failed,
                en_riesgo=r_risk,
                progreso_pct=round(progreso, 1),
                red_visitas=r_red,
                vip_visitas=r_vip_count,
                high_priority=r_high,
                visitas=visitas_out,
            ))
            e_total += total; e_comp += comp; e_pend += pend
            e_failed += r_failed; e_risk += r_risk; e_red += r_red
            e_vip += r_vip_count; e_high += r_high

        out_empresas.append(PlanEmpresaNew(
            empresa_id=int(eid),
            empresa_nombre=empresas_catalog.get(int(eid), f"Empresa {eid}"),
            total_visitas=e_total,
            completadas=e_comp,
            pendientes=e_pend,
            fallidas=e_failed,
            en_riesgo=e_risk,
            red_visitas=e_red,
            vip_visitas=e_vip,
            high_priority=e_high,
            rutas=rutas_out,
        ))
    out_empresas.sort(key=lambda e: e.empresa_id)
    return PlanDiarioResponseNew(
        planned_date=STATE.today.isoformat(),
        sim_clock=STATE.sim_clock.isoformat(),
        region=region,
        only_vip=only_vip,
        source="synthetic",
        empresas=out_empresas,
    )


def _is_at_risk_synth(row) -> bool:
    if row["status"] != "pending":
        return False
    if str(row.get("alert_slack", "")) == "RED":
        return True
    if bool(row.get("alert_valuedata", False)):
        return True
    try:
        if float(row.get("p_fallo", 0)) >= 0.5:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


# =============================================================================
# Builder: legacy (Sprint 1) — Empresa → Drivers → Visits
# =============================================================================
def _build_legacy_from_snapshot(
    empresa_id: Optional[int],
    region: str,
) -> PlanDiarioResponseLegacy:
    if STATE.snapshot_df is None or STATE.today is None or STATE.sim_clock is None:
        raise HTTPException(503, "Backend warming up")
    df = STATE.snapshot_df.copy()
    df["empresa_id"] = df["vehicle_id"].astype(int).map(STATE.vehicle_empresa_map)
    if empresa_id is not None:
        df = df[df["empresa_id"] == empresa_id]
    df["region"] = df.apply(lambda r: _visit_region(r.get("latitude"), r.get("longitude")), axis=1)
    if region == "RM":
        df = df[df["region"] == "RM"]
    elif region == "regiones":
        df = df[df["region"] != "RM"]

    driver_by_vid = {int(v["vehicle_id"]): v["driver_name"] for v in STATE.vehicles_ext}
    tids = df["tracking_id"].astype(str).unique().tolist()
    titles = df["title"].astype(str).unique().tolist()
    priority_map = _load_priority_overrides(tids)
    vip_map = _load_vip_match(titles)
    empresas_catalog = {int(e["empresa_id"]): e["nombre"] for e in STATE.empresas}

    out_empresas: list[PlanEmpresaLegacy] = []
    for eid, edf in df.groupby("empresa_id"):
        drivers_out: list[PlanDriverLegacy] = []
        e_total = e_comp = e_pend = e_red = e_vip = e_high = 0
        for vid, vdf in edf.groupby("vehicle_id"):
            vdf_sorted = vdf.sort_values("order")
            visits_out: list[PlanVisitLegacy] = []
            v_red = v_vip_count = v_high = 0
            for _, row in vdf_sorted.iterrows():
                tid = str(row["tracking_id"])
                title = str(row["title"])
                prio, reason = priority_map.get(tid, ("normal", None))
                is_vip = title in vip_map
                if is_vip and prio in ("normal", "low"):
                    prio = "vip"
                if prio == "vip":
                    v_vip_count += 1
                if prio == "high":
                    v_high += 1
                if str(row.get("alert_slack", "")) == "RED" and row["status"] == "pending":
                    v_red += 1
                visits_out.append(PlanVisitLegacy(
                    tracking_id=tid,
                    order=int(row["order"]),
                    title=title,
                    address=str(row.get("address", "")),
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    window_start=str(row.get("window_start", "")),
                    window_end=str(row.get("window_end", "")),
                    planned_arrival_time=str(row.get("planned_arrival_time", "")),
                    estimated_time_arrival=str(row.get("estimated_time_arrival", "")),
                    slack_min=float(row.get("slack_min", 0.0)),
                    alert_slack=str(row.get("alert_slack", "")),
                    p_fallo=float(row.get("p_fallo", 0.0)),
                    status=str(row["status"]),
                    priority=prio,
                    priority_reason=reason,
                    is_vip=is_vip,
                    alert_valuedata=bool(row.get("alert_valuedata", False)),
                ))
            total = int(len(vdf_sorted))
            comp = int((vdf_sorted["status"] == "completed").sum())
            pend = total - comp
            drivers_out.append(PlanDriverLegacy(
                vehicle_id=int(vid),
                vehicle_name=str(vdf_sorted["vehicle_name"].iloc[0]),
                driver_name=driver_by_vid.get(int(vid), f"Driver {vid}"),
                total_visits=total,
                completed=comp,
                pending=pend,
                red_visits=v_red,
                vip_visits=v_vip_count,
                high_priority=v_high,
                visits=visits_out,
            ))
            e_total += total; e_comp += comp; e_pend += pend
            e_red += v_red; e_vip += v_vip_count; e_high += v_high

        drivers_out.sort(key=lambda d: d.vehicle_id)
        out_empresas.append(PlanEmpresaLegacy(
            empresa_id=int(eid),
            nombre=empresas_catalog.get(int(eid), f"Empresa {eid}"),
            total_visits=e_total, completed=e_comp, pending=e_pend,
            red_visits=e_red, vip_visits=e_vip, high_priority=e_high,
            drivers=drivers_out,
        ))
    out_empresas.sort(key=lambda e: e.empresa_id)
    return PlanDiarioResponseLegacy(
        planned_date=STATE.today.isoformat(),
        sim_clock=STATE.sim_clock.isoformat(),
        empresas=out_empresas,
    )

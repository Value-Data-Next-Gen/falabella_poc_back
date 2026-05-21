"""Plan diario: vista jerárquica Empresa → Ruta → Visitas ordenadas.

Fuente única (post Fase-2 MVP refactor): `fpoc.simpli_visits`.

Sprint 2: la respuesta agrupa por Empresa → Rutas → Visitas con orden y
progreso. Sprint 6 cambia la entidad ruta a `ruta_id` (R-YYYYMMDD-NNN).

Modo compat `?legacy=true` devuelve la forma vieja (Empresa → Drivers → Visits),
también leyendo de DB real (antes caía a snapshot sintético del modelo XGB
ahora eliminado).

Filtros:
  empresa_id   : admin/ops puede filtrar (transport_manager fixed a su empresa)
  region       : 'all' | 'RM' | 'regiones' (lee columna `region` del dataset)
  only_vip     : true => solo visitas que matchean fpoc_vip_clients
  planned_date : opcional 'YYYY-MM-DD' (default: STATE.today, o MAX si no existe)
  source       : solo 'real' (el modo 'synthetic' quedó eliminado en Fase 2)

Salida (estructura nueva Sprint 6):
  empresas[].rutas[] => { ruta_id, patente, driver_name, region, ct, ... }
  visitas[]          => { tracking_id, folio, current_eta_cl, region, comuna, ... }
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as date_cls, datetime
from typing import Optional, Union

import hashlib
import math
import random

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.cache import ttl_cached
from routers.comments import _visit_region
from core.db import get_conn
from core.state import STATE

router = APIRouter(prefix="/api/plan-diario", tags=["plan-diario"])

# Centroides aproximados (lat, lon) por comuna. Para fallback visual del mapa
# cuando fpoc.simpli_visits no trae lat/lon discretos (sólo comuna/region).
# Migrado desde el extinto sims.driver_sim como parte del refactor MVP Fase 2.
COMUNA_CENTROIDS = {
    # RM
    "Santiago": (-33.4489, -70.6693),
    "Santiago Centro": (-33.4489, -70.6693),
    "Las Condes": (-33.4137, -70.5807),
    "Providencia": (-33.4313, -70.6093),
    "Ñuñoa": (-33.4565, -70.5944),
    "Maipú": (-33.5111, -70.7580),
    "Puente Alto": (-33.6111, -70.5755),
    "La Florida": (-33.5226, -70.5984),
    "Vitacura": (-33.3892, -70.5734),
    "Lo Barnechea": (-33.3517, -70.5169),
    "La Reina": (-33.4474, -70.5400),
    "Peñalolén": (-33.4860, -70.5333),
    "Peñalolen": (-33.4860, -70.5333),
    "Macul": (-33.4860, -70.5944),
    "San Miguel": (-33.4986, -70.6543),
    "Quilicura": (-33.3589, -70.7290),
    "San Bernardo": (-33.5917, -70.7000),
    "La Cisterna": (-33.5325, -70.6644),
    "El Bosque": (-33.5614, -70.6743),
    "La Pintana": (-33.5878, -70.6342),
    "Independencia": (-33.4189, -70.6645),
    "Recoleta": (-33.4070, -70.6440),
    "Estación Central": (-33.4569, -70.6886),
    "Cerrillos": (-33.4953, -70.7106),
    "Pudahuel": (-33.4400, -70.7700),
    "Renca": (-33.4081, -70.7264),
    "Lo Espejo": (-33.5256, -70.6878),
    "San Joaquín": (-33.4956, -70.6308),
    "Huechuraba": (-33.3625, -70.6403),
    "Conchalí": (-33.3837, -70.6700),
    "Quinta Normal": (-33.4286, -70.7000),
    "Lo Prado": (-33.4444, -70.7261),
    "Cerro Navia": (-33.4192, -70.7375),
    "Pedro Aguirre Cerda": (-33.4892, -70.6711),
    # Regiones
    "Valparaíso": (-33.0472, -71.6127),
    "Viña del Mar": (-33.0153, -71.5500),
    "Concepción": (-36.8201, -73.0444),
    "Talcahuano": (-36.7250, -73.1153),
    "Temuco": (-38.7359, -72.5904),
    "La Serena": (-29.9027, -71.2519),
    "Coquimbo": (-29.9534, -71.3436),
    "Antofagasta": (-23.6500, -70.4000),
    "Talca": (-35.4264, -71.6553),
    "Rancagua": (-34.1708, -70.7444),
    "Curicó": (-34.9847, -71.2394),
}
DEFAULT_LATLON = (-33.4489, -70.6693)  # Santiago


_COMUNA_LATLON_BY_LOWER = {k.lower(): v for k, v in COMUNA_CENTROIDS.items()}
# Comunas adicionales que el XLSX real menciona pero faltaban en el dict de
# driver_sim. Mantener acá hasta que la fuente canónica viva en BD/seed.
_COMUNA_LATLON_BY_LOWER.update({
    "la dehesa": (-33.3517, -70.5169),  # sector de Lo Barnechea/Vitacura
})


def _comuna_to_latlon(comuna: Optional[str], tracking_id: Optional[str]) -> tuple[float, float]:
    """Centroide de comuna + jitter determinístico (~±1km) por tracking_id.

    Necesario para renderizar visitas del XLSX real en el mapa: el dataset real
    no incluye lat/lon, solo comuna. Sin esto los pines caen en (0, 0) y el
    frontend los filtra por bbox de Chile.

    El jitter es determinístico (seed=tracking_id) para que las posiciones no
    se muevan entre fetches consecutivos del frontend.

    Lookup case-insensitive: `.title()` rompía "Viña del Mar" ("del" capitaliza
    a "Del" y dejaba de matchear el dict).
    """
    if not comuna:
        return (0.0, 0.0)
    base = _COMUNA_LATLON_BY_LOWER.get(comuna.strip().lower())
    if base is None:
        # Sin comuna conocida → devolvemos (0, 0) y el frontend filtra.
        # No usamos DEFAULT_LATLON porque clavaría todos los stops "huérfanos"
        # en Santiago y se vería como un cluster falso.
        return (0.0, 0.0)
    seed = int(hashlib.md5((tracking_id or comuna).encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    return (base[0] + rng.uniform(-0.01, 0.01),
            base[1] + rng.uniform(-0.01, 0.01))


# =============================================================================
# Schemas — nueva estructura (Sprint 6: ruta_id como string)
# =============================================================================
class AlertEvent(BaseModel):
    """CR-012 T0.3: evento histórico de alerta enviada para una visita.

    Hidratado desde `fpoc_alert_dispatch_log` por tracking_id. El frontend lo
    muestra en el slide-over de drill-down (Tarea 8) en orden cronológico
    descendente. `acknowledged_at = None` y `sent_at` >10min atrás → estado
    'sin respuesta'."""
    timestamp: str       # ISO 8601 = alias de sent_at
    type: str            # 'retraso_vip' | 'driver_sin_respuesta' | 'motivo_patron'
    channel: str         # 'whatsapp' | 'sms' | 'in_app'
    target: str          # 'cliente' | 'driver' | 'supervisor'
    acknowledged_at: Optional[str] = None


# Mock fallback cuando un driver/supervisor no tiene phone_e164 configurado.
# El frontend respeta el flag is_mock para deshabilitar botones de WhatsApp
# directo y mostrar tooltip "teléfono no configurado".
_MOCK_PHONE = "+56 9 0000 0000"


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
    # CR-012 T0.3: historial de alertas enviadas para esta visita.
    alert_history: list[AlertEvent] = []


class PlanRuta(BaseModel):
    ruta_id: str                 # NEW Sprint 6: 'R-YYYYMMDD-NNN'
    vehicle_id: int              # = patente_falsa (compat)
    vehicle_name: str            # 'FAL-XXXX' (synth) o 'PAT-NNN' (real)
    plate: Optional[str] = None
    patente: Optional[str] = None  # NEW alias
    # driver_name puede venir None desde DB (visitas sin chofer asignado, dotación
    # incompleta). Antes era str estricto y el endpoint 500eaba con
    # pydantic.ValidationError. Renderizamos "—" en el frontend si es None.
    driver_name: Optional[str] = None
    dotacion_estado: Optional[str] = None
    dotacion_motivo: Optional[str] = None
    operable: bool = True
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
    # CR-012 T0.3: teléfono del driver para acción "Contactar driver" del
    # drawer de Operación. is_mock=True cuando fpoc_drivers.phone_e164 es NULL
    # → el frontend deshabilita botón WhatsApp y muestra tooltip.
    driver_phone: Optional[str] = None
    driver_phone_is_mock: bool = False


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
    # CR-012 T0.3: teléfono supervisor (escalamiento). Mock si la empresa no
    # tiene supervisor_phone_e164 — el modal de escalamiento deshabilita Enviar.
    supervisor_phone: Optional[str] = None
    supervisor_phone_is_mock: bool = False


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
    vehicle_name: Optional[str] = None
    driver_name: Optional[str] = None
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
                from routers.comments import _resolve_alert_config
                for r in cur.fetchall():
                    eid = int(r.empresa_id) if r.empresa_id is not None else None
                    _alertable, sev = _resolve_alert_config(r.motivo, eid)
                    out[r.tracking_id] = (r.motivo, sev)
    except Exception:  # noqa: BLE001
        pass
    return out


def _load_driver_phones() -> dict[str, str]:
    """CR-012 T0.3: driver name normalizado → phone_e164. Solo no-NULL.

    Match por nombre porque `fpoc_simpli_visits.driver_name` no expone driver_id.
    Caso: dos drivers con mismo nombre → gana el primero (improbable en POC).
    """
    out: dict[str, str] = {}
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT name, phone_e164 FROM fpoc.drivers "
                "WHERE phone_e164 IS NOT NULL AND phone_e164 != ''"
            )
            for r in cur.fetchall():
                key = str(r.name).strip().lower()
                if key not in out:  # primero gana
                    out[key] = str(r.phone_e164)
    except Exception:
        # Tabla recién migrada / sin datos. Vacío → todos los drivers serán mock.
        pass
    return out


def _load_alert_history(tids: list[str]) -> dict[str, list[AlertEvent]]:
    """CR-012 T0.3: tracking_id → lista cronológica ascendente de alertas
    enviadas. Lee `fpoc.alert_dispatch_log`. Si la tabla no existe (caso
    pre-migración) devuelve dict vacío y la API responde alert_history=[]."""
    out: dict[str, list[AlertEvent]] = {}
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
                    SELECT tracking_id, type, channel, target, sent_at, acknowledged_at
                    FROM fpoc.alert_dispatch_log
                    WHERE tracking_id IN ({marks})
                    ORDER BY sent_at ASC
                    """,
                    *batch,
                )
                for r in cur.fetchall():
                    ev = AlertEvent(
                        timestamp=str(r.sent_at),
                        type=str(r.type),
                        channel=str(r.channel),
                        target=str(r.target),
                        acknowledged_at=str(r.acknowledged_at) if r.acknowledged_at else None,
                    )
                    out.setdefault(str(r.tracking_id), []).append(ev)
    except Exception:
        pass
    return out


def _load_dotacion(planned_date: str) -> tuple[dict[int, dict], dict[str, dict]]:
    """Daily availability by vehicle_id and by normalized driver_name."""
    by_vehicle: dict[int, dict] = {}
    by_driver_name: dict[str, dict] = {}
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT dd.vehicle_id, dd.driver_id, dd.estado, dd.motivo, d.name AS driver_name
                FROM fpoc.dotacion_diaria dd
                LEFT JOIN fpoc.drivers d ON d.driver_id = dd.driver_id
                WHERE dd.fecha = ?
                """,
                planned_date,
            )
            for r in cur.fetchall():
                item = {
                    "estado": str(r.estado),
                    "motivo": str(r.motivo) if r.motivo else None,
                    "driver_id": str(r.driver_id) if r.driver_id else None,
                }
                if r.vehicle_id is not None:
                    by_vehicle[int(r.vehicle_id)] = item
                if r.driver_name:
                    by_driver_name[str(r.driver_name).strip().lower()] = item
    except Exception:  # noqa: BLE001
        pass
    return by_vehicle, by_driver_name


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
# CR-012 T0.3: response_model declarado como Union para que openapi.json
# exponga ambos schemas (NEW y Legacy) — sin esto los nuevos campos no llegan
# al frontend via gen-types. Pydantic distingue por presencia de `rutas`
# (NEW, default) vs `drivers` (Legacy, opt-in) en empresas[].
#
# CONTRATO (CR-012 Fix V3):
#   ?legacy=false (default) → PlanDiarioResponseNew (Sprint 6+: ruta_id como
#                             entidad, alert_history, driver_phone, etc.).
#                             Consumido por: KpiStrip, OperationsMap, MapaTab,
#                             DriversAvancePanel, GanttPorParada, VisitaDetailDrawer.
#   ?legacy=true            → PlanDiarioResponseLegacy (Sprint 1: Empresa→Drivers).
#                             Consumido por: RouteOpsPanel (frontend/src/components/
#                             RouteOpsPanel.tsx:93 — `plan-diario-legacy` queryKey).
#                             Solo lo usa esa pantalla.
#
# TODO(deprecate-legacy): cuando RouteOpsPanel migre al shape NEW (estimado
# 2026-Q3, ver ROADMAP), eliminar:
#   1) la rama `if legacy:` de este handler,
#   2) las clases `PlanVisitLegacy`, `PlanEmpresaLegacy`, `PlanDiarioResponseLegacy`,
#   3) el query param `legacy: bool`,
#   4) volver el response_model a `PlanDiarioResponseNew` directo.
@router.get("", response_model=Union[PlanDiarioResponseNew, PlanDiarioResponseLegacy])
def get_plan_diario(
    empresa_id: Optional[int] = Query(default=None),
    region: str = Query(default="all"),
    only_vip: bool = Query(default=False),
    legacy: bool = Query(default=False),
    source: str = Query(default="real", pattern="^(real)$"),
    planned_date: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
):
    """Plan diario. Fuente única: fpoc.simpli_visits.

    Tras Fase 2 MVP refactor el modo `source=synthetic` quedó eliminado
    (acoplado a STATE.snapshot_df que ya no existe). El query param se mantiene
    por compat de URL pero solo acepta `real`.

    Modo `legacy=true` sigue exponiendo el shape Sprint-1 (Empresa→Drivers→Visits),
    leyendo SIEMPRE de DB real (antes caía al snapshot sintético cuando no venía
    planned_date; ahora usa STATE.today como default).
    """
    # Scope
    if not user.is_falabella:
        empresa_id = user.empresa_id

    if legacy:
        target_date = planned_date or (STATE.today.isoformat() if STATE.today else None)
        if not target_date:
            raise HTTPException(503, "No hay día operativo activo (planned_date requerido)")
        return _build_legacy_from_real(empresa_id, region, target_date)

    return _build_new_from_real(empresa_id, region, only_vip, planned_date)


# =============================================================================
# Builder: dataset REAL (fpoc_simpli_visits) — Sprint 6
# =============================================================================
# Cache TTL 30s: el endpoint tarda ~50s contra Azure SQL en cache miss. Sin
# este buffer, cada request del frontend (polling 10s) hace query nueva y se
# apilan. Con TTL 30s, ~3 polls consecutivos del front comparten resultado.
# Cambios reales en BD invalidan la cache vía `_invalidate_state_caches()`
# desde los mutators (transition/regenerate/reset/clean-and-regenerate).
@ttl_cached(ttl_seconds=30)
def _build_new_from_real(
    empresa_id: Optional[int],
    region: str,
    only_vip: bool,
    planned_date_q: Optional[str],
) -> PlanDiarioResponseNew:
    """Lee fpoc_simpli_visits, agrupa por empresa → ruta_id, devuelve estructura nueva."""
    pd_iso = _resolve_planned_date(planned_date_q)

    # Empresas catalog + supervisor_phone_e164 (CR-012 T0.3)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT empresa_id, nombre, supervisor_phone_e164 "
            "FROM fpoc.empresas_transporte WHERE activo = 1"
        )
        empresas_catalog: dict[int, dict] = {}
        for r in cur.fetchall():
            sup_e164 = getattr(r, "supervisor_phone_e164", None)
            empresas_catalog[int(r.empresa_id)] = {
                "nombre": r.nombre,
                "supervisor_phone": sup_e164 or _MOCK_PHONE,
                "supervisor_phone_is_mock": not bool(sup_e164),
            }

        # WHERE clauses
        where = ["planned_date = ?"]
        params: list = [pd_iso]
        if empresa_id is not None:
            where.append("empresa_falsa = ?")
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
                   empresa_falsa, patente_falsa, driver_name, fecha_inicio_ruta_hora_cl
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
            "order": int(getattr(r, "order")) if hasattr(r, "order") else int(r["order"]),
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
            "empresa_id": int(r.empresa_falsa),
            "patente": int(r.patente_falsa),
            "drivername": str(r.driver_name) if r.driver_name else "",
            "fecha_inicio_hora": str(r.fecha_inicio_ruta_hora_cl) if r.fecha_inicio_ruta_hora_cl else "",
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
    dotacion_by_vehicle, dotacion_by_driver = _load_dotacion(pd_iso)
    # CR-012 T0.3: phones + alert history (best-effort, mock si vacío)
    driver_phones = _load_driver_phones()
    alert_history_map = _load_alert_history(tids)

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
            region_count: dict[str, int] = {}

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
                _reg = vd["region"] or "RM"
                region_count[_reg] = region_count.get(_reg, 0) + 1

                folio = f"#{vd['reference']}" if vd.get("reference") else None

                # Lat/lon desde centroide de comuna (jitter determinístico por tid)
                # — habilita renderizar visitas reales en el mapa.
                v_lat, v_lon = _comuna_to_latlon(vd["comuna"], tid)

                visitas_out.append(PlanVisit(
                    tracking_id=tid,
                    order=vd["order"],
                    title=title,
                    cliente_nombre=title,
                    address=vd["address"],
                    comuna=vd["comuna"],
                    region=vd["region"],
                    latitude=v_lat, longitude=v_lon, lat=v_lat, lon=v_lon,
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
                    alert_history=alert_history_map.get(tid, []),
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
            # Región dominante = más frecuente entre los stops de la ruta.
            # Antes era "la primera del set" (arbitraria por orden de iteración),
            # lo que producía rutas declaradas en Araucanía/Coquimbo con 11 de 12
            # stops en RM y el filtro client-side descartando los stops "buenos".
            region_dom = (
                max(region_count.items(), key=lambda kv: kv[1])[0]
                if region_count else "RM"
            )
            dotacion = (
                dotacion_by_vehicle.get(int(sample["patente"]))
                or dotacion_by_driver.get(str(sample["drivername"]).strip().lower())
            )
            dotacion_estado = dotacion.get("estado") if dotacion else None
            dotacion_motivo = dotacion.get("motivo") if dotacion else None
            operable = dotacion_estado in (None, "disponible", "reemplazo")

            driver_full = sample["drivername"] or f"Driver {sample['patente']}"
            driver_phone_real = driver_phones.get(driver_full.strip().lower())
            rutas_out.append(PlanRuta(
                ruta_id=rid,
                vehicle_id=sample["patente"],
                vehicle_name=patente_str,
                plate=patente_str,
                patente=patente_str,
                driver_name=driver_full,
                dotacion_estado=dotacion_estado,
                dotacion_motivo=dotacion_motivo,
                operable=operable,
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
                driver_phone=driver_phone_real or _MOCK_PHONE,
                driver_phone_is_mock=not bool(driver_phone_real),
            ))
            e_total += total
            e_comp += comp
            e_pend += pend
            e_failed += failed
            e_risk += r_risk
            e_red += r_red
            e_vip += r_vip_count
            e_high += r_high

        emp_info = empresas_catalog.get(eid, {})
        out_empresas.append(PlanEmpresaNew(
            empresa_id=eid,
            empresa_nombre=emp_info.get("nombre", f"Empresa {eid}"),
            total_visitas=e_total,
            completadas=e_comp,
            pendientes=e_pend,
            fallidas=e_failed,
            en_riesgo=e_risk,
            red_visitas=e_red,
            vip_visitas=e_vip,
            high_priority=e_high,
            rutas=rutas_out,
            supervisor_phone=emp_info.get("supervisor_phone", _MOCK_PHONE),
            supervisor_phone_is_mock=emp_info.get("supervisor_phone_is_mock", True),
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


# Builder: legacy (Sprint 1) — Empresa → Drivers → Visits, leyendo DB real.
# Añadido en R8 para que el Copiloto (RouteOpsPanel) lea las visitas del
# XLSX importado en lugar del snapshot sintético.
# =============================================================================
def _build_legacy_from_real(
    empresa_id: Optional[int],
    region: str,
    planned_date: str,
) -> PlanDiarioResponseLegacy:
    from datetime import date as _date_cls
    try:
        _date_cls.fromisoformat(planned_date)
    except ValueError:
        raise HTTPException(400, f"planned_date inválida: {planned_date}")

    where = ["planned_date = ?"]
    params: list = [planned_date]
    if empresa_id is not None:
        where.append("empresa_falsa = ?")
        params.append(empresa_id)
    if region == "RM":
        where.append("region = 'RM'")
    elif region == "regiones":
        where.append("(region IS NOT NULL AND region <> 'RM')")

    with get_conn() as cn:
        cur = cn.cursor()
        # planned_start/planned_end NO existen en fpoc.simpli_visits — eran
        # columnas asumidas que rompían el endpoint con 500 silencioso
        # (Probador IA quedaba "sin drivers"). Si las necesitamos a futuro
        # se derivan de current_eta_cl o se agregan al schema.
        cur.execute(
            f"""SELECT id AS tracking_id, "order", title, address,
                       empresa_falsa AS empresa_id, patente_falsa AS vehicle_id,
                       driver_name, status, current_eta_cl,
                       region
                FROM fpoc.simpli_visits
                WHERE {' AND '.join(where)}
                ORDER BY empresa_falsa, patente_falsa, "order" """,
            *params,
        )
        rows = cur.fetchall()
        cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte")
        empresas_catalog = {int(r.empresa_id): r.nombre for r in cur.fetchall()}

    # VIPs
    titles = list({str(r.title) for r in rows if r.title})
    vip_map = _load_vip_match(titles)
    priority_map = _load_priority_overrides([str(r.tracking_id) for r in rows])

    # Group by empresa→driver
    from collections import defaultdict
    empresa_groups: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        eid = int(r.empresa_id) if r.empresa_id is not None else 0
        vid = int(r.vehicle_id) if r.vehicle_id is not None else 0
        empresa_groups[eid][vid].append(r)

    out_empresas: list[PlanEmpresaLegacy] = []
    for eid, by_vid in empresa_groups.items():
        drivers_out: list[PlanDriverLegacy] = []
        e_total = e_comp = e_pend = e_red = e_vip = e_high = 0
        for vid, vrows in by_vid.items():
            visits_out: list[PlanVisitLegacy] = []
            v_red = v_vip_count = v_high = 0
            driver_name = ""
            for r in vrows:
                if r.driver_name and not driver_name:
                    driver_name = str(r.driver_name)
                tid = str(r.tracking_id)
                title = str(r.title or "")
                prio, reason = priority_map.get(tid, ("normal", None))
                is_vip = title in vip_map
                if is_vip and prio in ("normal", "low"):
                    prio = "vip"
                if prio == "vip": v_vip_count += 1
                if prio == "high": v_high += 1
                status_s = str(r.status or "pending")
                # alert_slack/p_fallo no están en simpli_visits → defaults
                visits_out.append(PlanVisitLegacy(
                    tracking_id=tid,
                    order=int(getattr(r, "order")) if hasattr(r, "order") and getattr(r, "order") is not None else 0,
                    title=title,
                    address=str(r.address or ""),
                    latitude=0.0,
                    longitude=0.0,
                    window_start="",
                    window_end="",
                    planned_arrival_time="",  # planned_start no existe en schema actual
                    estimated_time_arrival=str(r.current_eta_cl)[11:16] if r.current_eta_cl else "",
                    slack_min=0.0,
                    alert_slack="GREEN",
                    p_fallo=0.0,
                    status=status_s,
                    priority=prio,
                    priority_reason=reason,
                    is_vip=is_vip,
                    alert_valuedata=False,
                ))
            total = len(vrows)
            comp = sum(1 for r in vrows if str(r.status) == "completed")
            pend = total - comp
            drivers_out.append(PlanDriverLegacy(
                vehicle_id=int(vid),
                vehicle_name=f"FAL-{vid}" if vid else "",
                driver_name=driver_name or f"Driver {vid}",
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

    sim_clock = (
        STATE.sim_clock.isoformat()
        if STATE.sim_clock is not None
        else f"{planned_date}T09:00:00"
    )
    return PlanDiarioResponseLegacy(
        planned_date=planned_date,
        sim_clock=sim_clock,
        empresas=out_empresas,
    )

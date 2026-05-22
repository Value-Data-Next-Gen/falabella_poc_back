"""Estado en memoria de la torre de control (post Fase-2 refactor MVP).

Tras el refactor MVP la única fuente de verdad para visitas es `fpoc.simpli_visits`.
El modelo ML XGBoost + SHAP + synthetic data generator quedó eliminado del
backend, y con eso desaparecen los atributos `snapshot_df`, `today_plan`,
`shap_vals`, `boot`, etc.

Lo único que sobrevive en STATE son **lookup tables** que el bot/LLM/handlers
de routers usan para enriquecer respuestas:

- `STATE.drivers`        : list[dict]  — desde fpoc.drivers
- `STATE.vehicles_ext`   : list[dict]  — desde fpoc.vehicles
- `STATE.empresas`       : list[dict]  — desde fpoc.empresas_transporte
- `STATE.vehicle_empresa_map` : dict[int, int]  vehicle_id → empresa_id
- `STATE.today`          : date | None — fecha operativa "actual" (gateada
                            por endpoints como /api/planificacion/start-day)
- `STATE.sim_clock`      : datetime | None — placeholder; Fase 3 lo populará
                            con interpolación basada en DB.

`reload_maestros()` se mantiene para que los CRUDs de drivers/vehicles
invaliden el cache.

`is_operational_day_active()` se mantiene (gateado por fpoc.planificacion_imports).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Union


@dataclass
class AppState:
    # Maestros (lookup tables que enriquecen respuestas del bot / routers)
    drivers: list[dict] = field(default_factory=list)
    vehicles_ext: list[dict] = field(default_factory=list)
    empresas: list[dict] = field(default_factory=list)

    # Estado del día operativo
    today: date | None = None
    sim_clock: datetime | None = None

    # vehicle_id -> empresa_id (mapeo determinístico para multi-tenancy POC)
    vehicle_empresa_map: dict[int, int] = field(default_factory=dict)

    # Auto-notify cooldown: timestamp (wall clock) del último envío por teléfono.
    # Reutilizado por vip_deadline_cron y otros notifiers para evitar spam.
    _autonotify_last_sent: dict[str, datetime] = field(default_factory=dict)

    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ----- Lifecycle -----
    def init(self) -> None:
        """Carga maestros desde DB. Sin modelo ML, sin plan sintético.

        Si la DB falla la app igual arranca con maestros vacíos (los routers
        que dependen de STATE.drivers / STATE.vehicles_ext caen graciosamente).
        """
        self._load_maestros()
        self._load_empresas_and_assign()
        self.today = date.today()
        self.sim_clock = datetime.utcnow()

    def _load_maestros(self) -> None:
        """Carga drivers/vehicles desde la DB. Si fallan, se quedan vacíos."""
        from core.db import get_conn
        from loguru import logger
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    """
                    SELECT driver_id, name, phone, license, empresa_id, vehicle_id, vehicle_name,
                           rating, deliveries_30d, fail_rate_30d, joined_at, active,
                           is_problem_hidden
                    FROM fpoc.drivers
                    ORDER BY vehicle_id
                    """
                )
                drivers = [
                    {
                        "driver_id": r.driver_id,
                        "name": r.name,
                        "phone": r.phone,
                        "license": r.license,
                        "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
                        "vehicle_id": int(r.vehicle_id),
                        "vehicle_name": r.vehicle_name,
                        "rating": float(r.rating),
                        "deliveries_30d": int(r.deliveries_30d),
                        "fail_rate_30d": float(r.fail_rate_30d),
                        "joined_at": r.joined_at if isinstance(r.joined_at, str) else (r.joined_at.isoformat() if r.joined_at else None),
                        "active": bool(r.active),
                        "is_problem_hidden": bool(r.is_problem_hidden),
                    }
                    for r in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT vehicle_id, empresa_id, name, type, plate, capacity_m3, driver_id, driver_name,
                           depot_lat, depot_lon, year, active, is_problem_hidden
                    FROM fpoc.vehicles
                    ORDER BY vehicle_id
                    """
                )
                vehicles = [
                    {
                        "vehicle_id": int(r.vehicle_id),
                        "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
                        "name": r.name,
                        "type": r.type,
                        "plate": r.plate,
                        "capacity_m3": int(r.capacity_m3),
                        "driver_id": r.driver_id,
                        "driver_name": r.driver_name,
                        "depot_lat": float(r.depot_lat),
                        "depot_lon": float(r.depot_lon),
                        "year": int(r.year) if r.year is not None else None,
                        "active": bool(r.active),
                        "is_problem_hidden": bool(r.is_problem_hidden),
                    }
                    for r in cur.fetchall()
                ]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[state] no pude cargar maestros desde DB: {e}. Quedan vacíos.")
            drivers, vehicles = [], []

        self.drivers = drivers
        self.vehicles_ext = vehicles

    def reload_maestros(self) -> None:
        """Re-lee drivers/vehicles desde la DB tras un CRUD."""
        with self._lock:
            self._load_maestros()
            self._load_empresas_and_assign()

    def _load_empresas_and_assign(self) -> None:
        """Carga empresas y arma vehicle_id -> empresa_id.

        Preferimos el ownership persistente de fpoc.vehicles.empresa_id. El
        round-robin queda solo como fallback para datos antiguos sin migrar.
        """
        from loguru import logger
        try:
            from core.db import get_conn
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT empresa_id, nombre FROM fpoc.empresas_transporte WHERE activo = 1 ORDER BY empresa_id"
                )
                rows = cur.fetchall()
            self.empresas = [{"empresa_id": int(r[0]), "nombre": r[1]} for r in rows]
            if not self.empresas:
                logger.warning("[state] fpoc_empresas_transporte vacía. Multi-tenancy deshabilitado.")
                self.empresas = [{"empresa_id": 0, "nombre": "Default"}]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[state] no pude cargar empresas: {e}. Multi-tenancy deshabilitado.")
            self.empresas = [{"empresa_id": 0, "nombre": "Default"}]

        vehicle_ids = sorted(int(v["vehicle_id"]) for v in self.vehicles_ext)
        n_empresas = len(self.empresas)
        if n_empresas == 0:
            self.vehicle_empresa_map = {}
            return
        fallback = {
            vid: self.empresas[i % n_empresas]["empresa_id"]
            for i, vid in enumerate(vehicle_ids)
        }
        self.vehicle_empresa_map = {}
        for v in self.vehicles_ext:
            vid = int(v["vehicle_id"])
            eid = v.get("empresa_id")
            self.vehicle_empresa_map[vid] = int(eid) if eid is not None else fallback[vid]

    def vehicle_ids_for_empresa(self, empresa_id: int | None) -> list[int]:
        if empresa_id is None:
            return list(self.vehicle_empresa_map.keys())
        return [vid for vid, eid in self.vehicle_empresa_map.items() if eid == empresa_id]

    # ----- Compat shims post Fase-2 MVP refactor -----
    # El modelo ML quedó eliminado; estos atributos quedaban referenciados desde
    # múltiples routers/handlers que vivían con `if STATE.snapshot_df is not None`
    # como fast-path. Mientras se completa la migración a fpoc.simpli_visits en
    # todos los call sites, devolvemos None / fallbacks para que ese branch sea
    # siempre el "no hay snapshot" path (DB-driven).
    @property
    def snapshot_df(self):  # type: ignore[no-untyped-def]
        return None

    @property
    def boot(self):  # type: ignore[no-untyped-def]
        return None

    @property
    def shap_vals(self):  # type: ignore[no-untyped-def]
        return None

    @property
    def clients_master(self):  # type: ignore[no-untyped-def]
        return []

    @property
    def day_seed(self) -> int:  # type: ignore[no-untyped-def]
        return 0

    @property
    def manual_incidents(self) -> dict:  # type: ignore[no-untyped-def]
        return {}


STATE = AppState()


# ============================================================================
# Sim clock (Fase 3 MVP — piloto controlable)
# ============================================================================
# Modelo:
#   sim_clock(fecha) = datetime.utcnow() + offset_min(fecha)
# offset_min vive en `fpoc.planificacion_imports.sim_clock_offset_min` (INT, DEFAULT 0).
# Modo automatico => offset == 0 => devuelve UTC now.
# Modo manual     => offset != 0 => UTC now + offset.
# El panel del piloto avanza/resetea el offset via /api/admin/pilot/clock.

def _to_iso_date(fecha: Union[date, str]) -> str:
    """Acepta date o ISO str. Devuelve YYYY-MM-DD."""
    if isinstance(fecha, date):
        return fecha.isoformat()
    return str(fecha)[:10]


def _read_offset(fecha_iso: str) -> int:
    """Lee sim_clock_offset_min de planificacion_imports para esa fecha.
    Si la fila no existe o la columna no esta (migracion no aplicada todavia),
    devuelve 0 (modo automatico)."""
    from core.db import get_conn
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT sim_clock_offset_min FROM fpoc.planificacion_imports WHERE fecha = ?",
                fecha_iso,
            )
            r = cur.fetchone()
        if r is None or r[0] is None:
            return 0
        return int(r[0])
    except Exception:  # noqa: BLE001
        # Tabla/columna no existe aun (boot pre-migracion). Fail-open: modo auto.
        return 0


def get_sim_clock(fecha: Union[date, str]) -> datetime:
    """Sim clock para la fecha en hora LOCAL Chile (naive).

    El piloto inserta `simpli_visits.current_eta_cl` como datetime naive en
    hora Chile (ej. 11:36). Si devolviéramos UTC acá, la comparación
    sim_clock vs eta sería incoherente (4h de diferencia). Devolvemos
    Chile time naive para que ambos vivan en la misma referencia temporal.
    """
    fecha_iso = _to_iso_date(fecha)
    offset = _read_offset(fecha_iso)
    try:
        from zoneinfo import ZoneInfo
        chile_now = datetime.now(ZoneInfo("America/Santiago")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        # Fallback: Chile suele ser UTC-4 (verano) o UTC-3 (invierno).
        # Usamos -4 como aproximación; el offset manual del piloto
        # absorbe inconsistencias.
        chile_now = datetime.utcnow() - timedelta(hours=4)
    return chile_now + timedelta(minutes=offset)


def advance_sim_clock(fecha: Union[date, str], minutes_delta: int) -> int:
    """Suma minutes_delta al offset del dia. Devuelve nuevo offset total.

    UPSERT-like: si la fila del dia no existe en planificacion_imports, la
    creamos con state=BORRADOR para que el offset persista (sino el GET
    siguiente devolveria 0). En la practica, el piloto se setupea con
    setup() que ya garantiza la fila — esto es un safety net.
    """
    from core.db import get_conn
    fecha_iso = _to_iso_date(fecha)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT sim_clock_offset_min FROM fpoc.planificacion_imports WHERE fecha = ?",
            fecha_iso,
        )
        r = cur.fetchone()
        if r is None:
            cur.execute(
                "INSERT INTO fpoc.planificacion_imports (fecha, count, state, sim_clock_offset_min) "
                "VALUES (?, 0, 'BORRADOR', ?)",
                fecha_iso, int(minutes_delta),
            )
            new_offset = int(minutes_delta)
        else:
            new_offset = int(r[0] or 0) + int(minutes_delta)
            cur.execute(
                "UPDATE fpoc.planificacion_imports SET sim_clock_offset_min = ? WHERE fecha = ?",
                new_offset, fecha_iso,
            )
        cn.commit()
    return new_offset


def reset_sim_clock(fecha: Union[date, str]) -> None:
    """Pone offset = 0 (modo automatico)."""
    from core.db import get_conn
    fecha_iso = _to_iso_date(fecha)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE fpoc.planificacion_imports SET sim_clock_offset_min = 0 WHERE fecha = ?",
            fecha_iso,
        )
        cn.commit()


# ============================================================================
# Day-state gate (usado por WhatsApp bot, CR-015)
# ============================================================================
def is_operational_day_active() -> bool:
    """True si el día operativo de hoy está EN_CURSO (o PAUSADO si existiera).

    La fuente de verdad es la columna `state` de `fpoc.planificacion_imports`
    para la fecha de hoy. Estados posibles (Ronda 3):
      BORRADOR | VALIDADO | EN_CURSO | CERRADO
    PAUSADO se mantiene en la whitelist por defensiva (alias futuro / Ronda 4).

    Si no hay fila para hoy o la query falla (tabla no existe, DB caída) →
    devolvemos True para no romper el bot. Las queries operativas son menos
    riesgosas que romper el canal entero.
    """
    try:
        from core.db import get_conn
        today = date.today().isoformat()
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT state FROM fpoc.planificacion_imports WHERE fecha = ?",
                today,
            )
            r = cur.fetchone()
        if r is None:
            return False
        state = str(r[0]).upper() if r[0] is not None else ""
        return state in ("EN_CURSO", "PAUSADO")
    except Exception:  # noqa: BLE001
        # Tabla no existe / DB caída → no gate (fail-open).
        return True

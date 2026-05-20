"""Helpers de lectura de visitas para el bot / LLM / handlers WhatsApp.

Single source of truth para *lecturas* desde el bot: **siempre** lee de
`fpoc.simpli_visits` (la tabla real importada del Excel / fuente operativa).

Motivación (CR sync-bot-data):
  - `STATE.snapshot_df` es el dataset *sintético* del simulador ML — solo
    debe consumirse desde features ML (p_fallo, alert_valuedata,
    `_auto_notify_alerts`, vip_deadline_cron, panel web operacional).
  - El bot/LLM/comandos WhatsApp deben mostrar la *realidad operativa* que
    matchea el dashboard de seguimiento. Cuando el bot mezclaba ambas fuentes
    daba totales inconsistentes (48 vs 101 para el mismo vehículo).

Mapeo de columnas `fpoc_simpli_visits` → dict del bot:
  - `id` (INT)            → `tracking_id` (str)
  - `title`               → `title`
  - `comuna`, `region`    → idem
  - `address`             → idem
  - `ruta_id`             → idem
  - `driver_name`         → idem
  - `patente_falsa` (INT) → `vehicle_id` (int)    [NO existe `vehicle_id` separado]
  - `f"PAT-{patente_falsa}"` → `vehicle_name` (str)  [derivado, no existe en DB]
  - `current_eta_cl`      → `eta`
  - `status`              → status (`pending` | `completed` | `failed`)
  - `reference`           → `folio` (int)

Campos que el bot esperaba del snapshot pero NO existen en DB:
  - `alert_valuedata`     → siempre None / False
  - `p_fallo`             → siempre None / 0.0
  - `window_end`          → no hay columna discreta; se devuelve "23:59"

Fallback: si DB cae con excepción, el caller queda libre de degradar a
`STATE.snapshot_df`. Estos helpers solo loggean WARN y devuelven [] / {}.
"""
from __future__ import annotations

from datetime import date as _date_cls
from typing import Optional

from loguru import logger

from core.db import get_conn


def _today_iso() -> str:
    return _date_cls.today().isoformat()


def _row_to_visit(r) -> dict:
    """Mapea una fila de fpoc_simpli_visits al shape esperado por los renders
    del bot. r es un Row con acceso por índice (driver SQLite/pyodbc shim)."""
    # Indices del SELECT canónico (mantener en sync con queries de abajo):
    # 0: id, 1: title, 2: comuna, 3: status, 4: current_eta_cl,
    # 5: patente_falsa, 6: address, 7: ruta_id, 8: driver_name, 9: region
    eta_raw = str(r[4]) if r[4] is not None else ""
    # current_eta_cl viene como 'YYYY-MM-DD HH:MM:SS' (timestamp). El bot
    # solo usa HH:MM, así que extraemos la hora si aparece.
    eta_short = eta_raw.split(" ")[1][:5] if " " in eta_raw else eta_raw[:5]
    patente = r[5]
    return {
        "tracking_id": str(r[0]),
        "title": str(r[1] or ""),
        "comuna": str(r[2] or ""),
        "status": str(r[3] or "pending"),
        "eta": eta_short or "—",
        # No tenemos window_end discreto en DB; placeholder estable para que
        # los renders existentes no rompan al hacer .[:5].
        "window_end": "23:59",
        # Campos ML (no aplican a fuente DB):
        "alert_valuedata": False,
        "p_fallo": 0.0,
        # Extra útiles para renders:
        "vehicle_id": int(patente) if patente is not None else None,
        "vehicle_name": f"PAT-{patente}" if patente is not None else "—",
        "address": str(r[6] or "") if len(r) > 6 else "",
        "ruta_id": str(r[7] or "") if len(r) > 7 else "",
        "driver_name": str(r[8] or "") if len(r) > 8 else "",
        "region": str(r[9] or "") if len(r) > 9 else "",
        # `order` legacy del snapshot — fallback a tracking_id como tie-breaker.
        "order": int(r[0]) if r[0] is not None else 0,
    }


def visits_for_vehicle_today(vehicle_id: Optional[int]) -> list[dict]:
    """Visitas de hoy (planned_date = CURRENT_DATE) para un vehículo.

    `vehicle_id` mapea a `patente_falsa` en fpoc_simpli_visits. Si es None
    devuelve []. Orden estable por `id ASC` (tracking_id numérico).
    Si la DB falla devuelve [] y loggea WARN.
    """
    if vehicle_id is None:
        return []
    today = _today_iso()
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT id, title, comuna, status, current_eta_cl,
                       patente_falsa, address, ruta_id, driver_name, region
                FROM fpoc.simpli_visits
                WHERE patente_falsa = ? AND planned_date = ?
                ORDER BY id
                """,
                (int(vehicle_id), today),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[_visits_db] visits_for_vehicle_today({vehicle_id}) DB fail: {e}"
        )
        return []
    return [_row_to_visit(r) for r in rows]


def visits_for_driver_today(driver_id: str) -> list[dict]:
    """Visitas de hoy para un driver (resolviendo driver_id → vehicle_id
    via fpoc_drivers). Si no hay vehículo asignado devuelve []."""
    driver_id = (driver_id or "").strip()
    if not driver_id:
        return []
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT vehicle_id FROM fpoc_drivers WHERE driver_id = ?",
                (driver_id,),
            )
            r = cur.fetchone()
        if r is None or r[0] is None:
            return []
        return visits_for_vehicle_today(int(r[0]))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[_visits_db] visits_for_driver_today({driver_id}) DB fail: {e}"
        )
        return []


def _kpis_query(where_extra: str = "", params: tuple = ()) -> dict:
    """Helper interno: corre la query de KPIs con un WHERE opcional adicional.
    Devuelve dict con total/pending/completed/failed (0 si DB cae)."""
    today = _today_iso()
    # nosec B608: `where_extra` es una constante interna (no llega del usuario).
    # Los únicos call-sites lo pasan vacío o como "AND empresa_falsa = ?" con
    # placeholder posicional + tupla params separada. Nunca se interpola input.
    sql = f"""
        SELECT
          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
          SUM(CASE WHEN status='pending'   THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
          COUNT(*) AS total
        FROM fpoc.simpli_visits
        WHERE planned_date = ? {where_extra}
    """  # nosec B608
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(sql, (today, *params))
            r = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[_visits_db] _kpis_query DB fail: {e}")
        return {"total": 0, "pending": 0, "completed": 0, "failed": 0}
    if r is None:
        return {"total": 0, "pending": 0, "completed": 0, "failed": 0}
    return {
        "total": int(r[3] or 0),
        "pending": int(r[1] or 0),
        "completed": int(r[0] or 0),
        "failed": int(r[2] or 0),
    }


def kpis_today() -> dict:
    """Totales del día (global) leídos de fpoc.simpli_visits.

    Shape: `{"total": N, "pending": N, "completed": N, "failed": N}`.
    A diferencia del snapshot_df, NO incluye `alerts` (alertas anticipadas
    son una feature exclusiva del simulador ML, no viven en DB).
    """
    return _kpis_query()


def kpis_today_by_empresa(empresa_id: Optional[int]) -> dict:
    """Idem `kpis_today` pero scopeado a una empresa (`empresa_falsa = ?`).
    Si `empresa_id` es None devuelve los KPIs globales."""
    if empresa_id is None:
        return kpis_today()
    return _kpis_query(
        where_extra="AND empresa_falsa = ?",
        params=(int(empresa_id),),
    )

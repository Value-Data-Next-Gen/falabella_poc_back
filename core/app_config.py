"""Runtime configuration con persistencia en DB.

Tabla `fpoc_app_config` (key TEXT PRIMARY KEY, value TEXT, updated_at, updated_by_user_id).

Se crea on-demand al primer acceso si no existe (idempotente).

Keys soportadas:
  - eta_window_hours    → float, default 2.0
  - alert_threshold     → float, default 0.50

Uso:
    from core.app_config import get_eta_window_hours, set_eta_window_hours
    h = get_eta_window_hours()              # lee config (cache + DB)
    set_eta_window_hours(3.5, user_id=42)   # persiste y actualiza cache
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from loguru import logger

from core.db import get_conn


_DEFAULTS: dict[str, float] = {
    "eta_window_hours": 2.0,
    "alert_threshold": 0.50,
}

# Caché en memoria: (value, mtime). Reload-on-write garantiza coherencia entre
# procesos no, pero en POC corre 1 worker uvicorn → suficiente.
_CACHE: dict[str, float] = {}
_LOCK = threading.Lock()
_INITIALIZED = False


def _ensure_table() -> None:
    """Crea la tabla si no existe. Idempotente en Azure SQL."""
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            IF OBJECT_ID('fpoc_app_config', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc_app_config (
                    [key] NVARCHAR(100) NOT NULL PRIMARY KEY,
                    value NVARCHAR(MAX) NOT NULL,
                    updated_at DATETIME2(0) NOT NULL DEFAULT SYSDATETIME(),
                    updated_by_user_id INT NULL
                )
            END
            """
        )
        cn.commit()


def _load_all() -> None:
    """Carga todas las keys de la DB al cache. Defaults si no existen."""
    global _INITIALIZED
    _ensure_table()
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT [key], value FROM fpoc_app_config")
        rows = cur.fetchall()
    db_values = {r[0]: r[1] for r in rows}
    for key, default in _DEFAULTS.items():
        raw = db_values.get(key)
        if raw is None:
            _CACHE[key] = float(default)
            continue
        try:
            _CACHE[key] = float(raw)
        except (TypeError, ValueError):
            logger.warning(f"[app_config] valor invalido en DB para {key}={raw!r}, uso default {default}")
            _CACHE[key] = float(default)
    _INITIALIZED = True


def _ensure_loaded() -> None:
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        _load_all()


def _get_float(key: str) -> float:
    _ensure_loaded()
    return float(_CACHE.get(key, _DEFAULTS[key]))


def _set_float(key: str, value: float, user_id: Optional[int]) -> float:
    _ensure_loaded()
    if key not in _DEFAULTS:
        raise ValueError(f"key desconocida: {key}")
    v = float(value)
    with _LOCK:
        with get_conn() as cn:
            cur = cn.cursor()
            # Upsert portátil sqlite/sqlserver (ON CONFLICT es sqlite-only).
            cur.execute("SELECT 1 FROM fpoc_app_config WHERE [key] = ?", (key,))
            if cur.fetchone():
                cur.execute(
                    """UPDATE fpoc_app_config
                          SET value = ?, updated_at = CURRENT_TIMESTAMP,
                              updated_by_user_id = ?
                        WHERE [key] = ?""",
                    (str(v), user_id, key),
                )
            else:
                cur.execute(
                    """INSERT INTO fpoc_app_config
                          ([key], value, updated_at, updated_by_user_id)
                         VALUES (?, ?, CURRENT_TIMESTAMP, ?)""",
                    (key, str(v), user_id),
                )
            cn.commit()
        _CACHE[key] = v
    logger.info(f"[app_config] {key} = {v} (by user_id={user_id})")
    return v


# ---------- Public API ----------

def get_eta_window_hours() -> float:
    return _get_float("eta_window_hours")


def set_eta_window_hours(value: float, user_id: Optional[int] = None) -> float:
    if value < 0.0 or value > 24.0:
        raise ValueError("eta_window_hours debe estar entre 0 y 24")
    return _set_float("eta_window_hours", value, user_id)


def get_alert_threshold() -> float:
    return _get_float("alert_threshold")


def set_alert_threshold(value: float, user_id: Optional[int] = None) -> float:
    if value < 0.0 or value > 1.0:
        raise ValueError("alert_threshold debe estar entre 0 y 1")
    return _set_float("alert_threshold", value, user_id)


def snapshot() -> dict[str, float]:
    """Devuelve el dict completo (para GET /api/admin/config)."""
    _ensure_loaded()
    return dict(_CACHE)


def get_audit_meta() -> dict[str, dict]:
    """Devuelve metadatos por key: updated_at, updated_by_user_id. Para UI."""
    _ensure_table()
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT [key], value, updated_at, updated_by_user_id FROM fpoc_app_config")
        rows = cur.fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[r[0]] = {
            "value": float(r[1]) if r[1] is not None else None,
            "updated_at": str(r[2]) if r[2] is not None else None,
            "updated_by_user_id": int(r[3]) if r[3] is not None else None,
        }
    # rellenar defaults para keys nunca seteadas
    for k, dflt in _DEFAULTS.items():
        if k not in out:
            out[k] = {"value": float(dflt), "updated_at": None, "updated_by_user_id": None}
    return out

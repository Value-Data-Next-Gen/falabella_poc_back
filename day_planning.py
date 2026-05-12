"""Endpoints de planificación del día por cliente + configuración específica.

GET  /api/planificacion/clientes-del-dia?fecha=YYYY-MM-DD
     Lista distinct clients del día con: # visitas, comunas, is_vip, vip_tier,
     notes del día (si existen). Para review masivo tras cargar XLSX.

PUT  /api/planificacion/client-day-notes
     Body: {fecha, cliente, notes?, vip_marked_here?}
     Upsert sobre client_day_notes. Si vip_marked_here=true, también crea
     fila en fpoc.vip_clients (match_type='title') si no existe.

GET  /api/planificacion/day-config?fecha=YYYY-MM-DD
PUT  /api/planificacion/day-config?fecha=YYYY-MM-DD
     Config específica del día.
"""
from __future__ import annotations

import json
from datetime import date as _date_cls
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from auth import CurrentUser, current_user, require_admin
from db import get_conn


router = APIRouter(tags=["day-planning"])


# ============================================================================
# Clientes del día
# ============================================================================
class ClienteDelDia(BaseModel):
    cliente: str
    visitas: int
    comunas: list[str]
    rutas: list[str]
    is_vip: bool
    vip_tier: Optional[str] = None
    vip_id: Optional[int] = None
    notes: Optional[str] = None
    vip_marked_here: bool = False
    priority_set_count: int = 0


@router.get("/api/planificacion/clientes-del-dia", response_model=list[ClienteDelDia])
def list_clientes_del_dia(
    fecha: str = Query(...),
    only_no_vip: bool = Query(default=False),
    only_with_notes: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> list[ClienteDelDia]:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    scope_where = ""
    scope_params: list = []
    if not user.is_falabella:
        scope_where = " AND s.empresa_falsa = ?"
        scope_params.append(user.empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()
        # Agregamos por title. STRING_AGG en SQL Server no soporta DISTINCT;
        # leemos todas las filas y dedupeamos en Python.
        cur.execute(
            f"""SELECT s.title AS cliente, s.comuna, s.ruta_id
                FROM fpoc.simpli_visits s
                WHERE s.planned_date = ?
                  AND s.title IS NOT NULL AND s.title <> ''
                  {scope_where}""",
            fecha, *scope_params,
        )
        raw_rows = cur.fetchall()
        # Agrupar en memoria
        agg: dict[str, dict] = {}
        for rr in raw_rows:
            t = str(rr.cliente)
            slot = agg.setdefault(t, {"visitas": 0, "comunas": set(), "rutas": set()})
            slot["visitas"] += 1
            if rr.comuna:
                slot["comunas"].add(str(rr.comuna))
            if rr.ruta_id:
                slot["rutas"].add(str(rr.ruta_id))
        agg_rows = [
            type("Row", (), {
                "cliente": t,
                "visitas": v["visitas"],
                "comunas": "|".join(sorted(v["comunas"])),
                "rutas": "|".join(sorted(v["rutas"])),
            })()
            for t, v in sorted(agg.items(), key=lambda kv: (-kv[1]["visitas"], kv[0]))
        ]

        # VIPs activos (match_type=title) — global + por empresa propia si manager
        cur.execute(
            "SELECT vip_id, match_value, tier, empresa_id "
            "FROM fpoc.vip_clients WHERE active = 1 AND match_type = 'title'"
        )
        vip_rows = cur.fetchall()
        vip_by_title: dict[str, dict] = {}
        for v in vip_rows:
            t = str(v.match_value)
            # Si tiene empresa_id, solo aplica para esa empresa. NULL = global.
            if v.empresa_id is None or (not user.is_falabella and int(v.empresa_id) == user.empresa_id) or user.is_falabella:
                vip_by_title[t] = {
                    "vip_id": int(v.vip_id),
                    "tier": str(v.tier) if v.tier else "VIP",
                }

        # Notes del día
        cur.execute(
            "SELECT cliente, notes, vip_marked_here FROM fpoc.client_day_notes WHERE fecha = ?",
            fecha,
        )
        notes_by_cliente: dict[str, dict] = {
            str(r.cliente): {"notes": r.notes, "vip_marked_here": bool(r.vip_marked_here)}
            for r in cur.fetchall()
        }

        # Priority overrides count por cliente (cuántas visitas de ese cliente tienen priority override)
        cur.execute(
            f"""SELECT s.title AS cliente, COUNT(*) AS n
                FROM fpoc.simpli_visits s
                INNER JOIN fpoc.visit_priority_overrides p ON p.tracking_id = CAST(s.id AS NVARCHAR(50))
                WHERE s.planned_date = ?{scope_where}
                GROUP BY s.title""",
            fecha, *scope_params,
        )
        priority_by_cliente: dict[str, int] = {str(r.cliente): int(r.n) for r in cur.fetchall()}

    out: list[ClienteDelDia] = []
    for r in agg_rows:
        cliente = str(r.cliente)
        vip = vip_by_title.get(cliente)
        notes_row = notes_by_cliente.get(cliente, {})
        is_vip = vip is not None
        if only_no_vip and is_vip:
            continue
        if only_with_notes and not notes_row.get("notes"):
            continue
        comunas = [c for c in (r.comunas or "").split("|") if c]
        rutas = [rr for rr in (r.rutas or "").split("|") if rr]
        out.append(ClienteDelDia(
            cliente=cliente,
            visitas=int(r.visitas or 0),
            comunas=comunas,
            rutas=rutas,
            is_vip=is_vip,
            vip_tier=vip["tier"] if vip else None,
            vip_id=vip["vip_id"] if vip else None,
            notes=notes_row.get("notes"),
            vip_marked_here=notes_row.get("vip_marked_here", False),
            priority_set_count=priority_by_cliente.get(cliente, 0),
        ))
    return out


# ============================================================================
# Notes por (fecha, cliente)
# ============================================================================
class ClientDayNoteIn(BaseModel):
    fecha: str
    cliente: str
    notes: Optional[str] = None
    vip_marked_here: Optional[bool] = None
    create_vip: bool = False
    vip_tier: Optional[str] = "VIP"


@router.put("/api/planificacion/client-day-notes")
def upsert_client_day_note(
    req: ClientDayNoteIn,
    user: CurrentUser = Depends(current_user),
) -> dict:
    try:
        _date_cls.fromisoformat(req.fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {req.fecha}")
    cliente = req.cliente.strip()
    if not cliente:
        raise HTTPException(400, "cliente vacío")

    vip_id = None
    with get_conn() as cn:
        cur = cn.cursor()
        # Si create_vip=true, asegurarse que exista en fpoc.vip_clients
        if req.create_vip:
            cur.execute(
                "SELECT vip_id FROM fpoc.vip_clients "
                "WHERE active=1 AND match_type='title' AND match_value = ? "
                "AND (empresa_id IS NULL OR empresa_id = ?)",
                cliente, user.empresa_id,
            )
            r = cur.fetchone()
            if r:
                vip_id = int(r.vip_id)
            else:
                empresa_id = None if user.is_falabella else user.empresa_id
                cur.execute(
                    "INSERT INTO fpoc.vip_clients "
                    "(match_type, match_value, empresa_id, tier, notes, active, created_by) "
                    "OUTPUT INSERTED.vip_id "
                    "VALUES ('title', ?, ?, ?, ?, 1, ?)",
                    cliente, empresa_id, req.vip_tier or "VIP",
                    f"Marcado desde Plan del día {req.fecha}", user.user_id,
                )
                rid = cur.fetchone()
                vip_id = int(rid[0]) if rid else None
                cn.commit()

        # Upsert client_day_notes
        cur.execute(
            "SELECT id FROM fpoc.client_day_notes WHERE fecha = ? AND cliente = ?",
            req.fecha, cliente,
        )
        existing = cur.fetchone()
        if existing:
            sets, params = [], []
            if req.notes is not None:
                sets.append("notes = ?"); params.append(req.notes)
            if req.vip_marked_here is not None:
                sets.append("vip_marked_here = ?"); params.append(1 if req.vip_marked_here else 0)
            sets.append("updated_at = SYSDATETIME()")
            sets.append("set_by_user_id = ?"); params.append(user.user_id)
            params.extend([req.fecha, cliente])
            cur.execute(
                f"UPDATE fpoc.client_day_notes SET {', '.join(sets)} "
                f"WHERE fecha = ? AND cliente = ?",
                *params,
            )
        else:
            cur.execute(
                "INSERT INTO fpoc.client_day_notes "
                "(fecha, cliente, notes, vip_marked_here, set_by_user_id) "
                "VALUES (?, ?, ?, ?, ?)",
                req.fecha, cliente, req.notes,
                1 if req.vip_marked_here else 0,
                user.user_id,
            )
        cn.commit()
    return {"ok": True, "fecha": req.fecha, "cliente": cliente, "vip_id": vip_id}


# ============================================================================
# Day config
# ============================================================================
class DayConfig(BaseModel):
    fecha: str
    cutoff_time: Optional[str] = None  # 'HH:MM' o 'HH:MM:SS'
    message_to_drivers: Optional[str] = None
    alert_threshold_override: Optional[float] = None
    slack_min_override: Optional[int] = None
    restricted_vehicle_ids: list[int] = []
    restricted_empresa_ids: list[int] = []
    updated_at: Optional[str] = None


def _row_to_day_config(fecha: str, r) -> DayConfig:
    if r is None:
        return DayConfig(fecha=fecha)
    def _parse_list(s) -> list[int]:
        if not s:
            return []
        try:
            v = json.loads(s)
            return [int(x) for x in v] if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            return []
    ct = r.cutoff_time
    cutoff_str = None
    if ct is not None:
        if hasattr(ct, "isoformat"):
            cutoff_str = ct.isoformat()
        else:
            cutoff_str = str(ct)
    return DayConfig(
        fecha=fecha,
        cutoff_time=cutoff_str,
        message_to_drivers=r.message_to_drivers,
        alert_threshold_override=float(r.alert_threshold_override) if r.alert_threshold_override is not None else None,
        slack_min_override=int(r.slack_min_override) if r.slack_min_override is not None else None,
        restricted_vehicle_ids=_parse_list(r.restricted_vehicle_ids),
        restricted_empresa_ids=_parse_list(r.restricted_empresa_ids),
        updated_at=str(r.updated_at) if r.updated_at else None,
    )


@router.get("/api/planificacion/day-config", response_model=DayConfig)
def get_day_config(
    fecha: str = Query(...),
    _: CurrentUser = Depends(current_user),
) -> DayConfig:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT cutoff_time, message_to_drivers, alert_threshold_override, "
            "slack_min_override, restricted_vehicle_ids, restricted_empresa_ids, "
            "updated_at FROM fpoc.day_config WHERE fecha = ?",
            fecha,
        )
        r = cur.fetchone()
    return _row_to_day_config(fecha, r)


class DayConfigUpdate(BaseModel):
    cutoff_time: Optional[str] = None
    message_to_drivers: Optional[str] = None
    alert_threshold_override: Optional[float] = None
    slack_min_override: Optional[int] = None
    restricted_vehicle_ids: Optional[list[int]] = None
    restricted_empresa_ids: Optional[list[int]] = None


@router.put("/api/planificacion/day-config", response_model=DayConfig)
def upsert_day_config(
    body: DayConfigUpdate,
    fecha: str = Query(...),
    user: CurrentUser = Depends(require_admin),
) -> DayConfig:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    if body.alert_threshold_override is not None:
        if body.alert_threshold_override < 0 or body.alert_threshold_override > 1:
            raise HTTPException(400, "alert_threshold_override debe estar entre 0 y 1")
    if body.slack_min_override is not None:
        if body.slack_min_override < 0 or body.slack_min_override > 240:
            raise HTTPException(400, "slack_min_override fuera de rango")
    if body.cutoff_time:
        # Normalizar HH:MM -> HH:MM:00
        parts = body.cutoff_time.strip().split(":")
        if len(parts) == 2:
            body.cutoff_time = f"{parts[0]}:{parts[1]}:00"
        elif len(parts) != 3:
            raise HTTPException(400, "cutoff_time formato HH:MM o HH:MM:SS")

    rvids = json.dumps(body.restricted_vehicle_ids) if body.restricted_vehicle_ids is not None else None
    reids = json.dumps(body.restricted_empresa_ids) if body.restricted_empresa_ids is not None else None

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT 1 FROM fpoc.day_config WHERE fecha = ?", fecha)
        if cur.fetchone():
            # UPDATE solo de campos no-None
            sets, params = [], []
            if body.cutoff_time is not None:
                sets.append("cutoff_time = ?"); params.append(body.cutoff_time)
            if body.message_to_drivers is not None:
                sets.append("message_to_drivers = ?"); params.append(body.message_to_drivers or None)
            if body.alert_threshold_override is not None:
                sets.append("alert_threshold_override = ?"); params.append(body.alert_threshold_override)
            if body.slack_min_override is not None:
                sets.append("slack_min_override = ?"); params.append(body.slack_min_override)
            if rvids is not None:
                sets.append("restricted_vehicle_ids = ?"); params.append(rvids)
            if reids is not None:
                sets.append("restricted_empresa_ids = ?"); params.append(reids)
            sets.append("updated_at = SYSDATETIME()")
            sets.append("set_by_user_id = ?"); params.append(user.user_id)
            params.append(fecha)
            cur.execute(f"UPDATE fpoc.day_config SET {', '.join(sets)} WHERE fecha = ?", *params)
        else:
            cur.execute(
                "INSERT INTO fpoc.day_config "
                "(fecha, cutoff_time, message_to_drivers, alert_threshold_override, "
                " slack_min_override, restricted_vehicle_ids, restricted_empresa_ids, "
                " set_by_user_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, SYSDATETIME())",
                fecha, body.cutoff_time, body.message_to_drivers,
                body.alert_threshold_override, body.slack_min_override,
                rvids, reids, user.user_id,
            )
        cn.commit()
        cur.execute(
            "SELECT cutoff_time, message_to_drivers, alert_threshold_override, "
            "slack_min_override, restricted_vehicle_ids, restricted_empresa_ids, "
            "updated_at FROM fpoc.day_config WHERE fecha = ?",
            fecha,
        )
        r = cur.fetchone()
    logger.info(f"[day-config] upsert {fecha} by user_id={user.user_id}")
    return _row_to_day_config(fecha, r)

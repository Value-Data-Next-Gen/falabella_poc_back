"""Endpoint del admin Falabella para intervenir un folio en vivo.

Acciones soportadas:
  - cancel             -> status='cancelled' + cancelled_at + notif a driver + manager
  - reschedule         -> nueva current_eta_cl + notif a driver + manager
  - escalate_priority  -> priority='HIGH' + notif a driver + manager
  - override_motivo    -> actualiza el motivo del último comment del folio +
                          notif a driver + manager

Cada intervención queda persistida en fpoc.visit_interventions para audit.

Auth: solo falabella_admin / falabella_ops (is_falabella). No accesible a
transport_manager (rompería la separación de roles: el manager NO interviene
fuera de su empresa).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import get_conn


router = APIRouter(prefix="/api/admin", tags=["admin-interventions"])


# ---------------------------------------------------------------------------
# Auth guard (idéntico al de admin_pilot)
# ---------------------------------------------------------------------------

def _require_admin_or_ops(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_falabella:
        raise HTTPException(403, "Requiere rol falabella_admin o falabella_ops")
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VisitInterventionRequest(BaseModel):
    tracking_id: str = Field(min_length=1, max_length=64)
    action: Literal["cancel", "reschedule", "escalate_priority", "override_motivo"]
    reason: Optional[str] = Field(default=None, max_length=500)
    # Args opcionales según action:
    new_eta: Optional[str] = None       # ISO datetime, requerido para reschedule
    new_motivo: Optional[str] = None    # requerido para override_motivo
    priority: Optional[str] = None      # 'HIGH' / 'NORMAL' (default HIGH para escalate)


class VisitInterventionResponse(BaseModel):
    intervention_id: Optional[int]
    tracking_id: str
    action: str
    driver_notified: bool
    manager_notified_count: int
    admin_notified_count: int
    before_value: Optional[str]
    after_value: Optional[str]
    detail: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/visit-intervention", response_model=VisitInterventionResponse)
def visit_intervention(
    req: VisitInterventionRequest,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> VisitInterventionResponse:
    """Aplica una intervención del admin sobre un folio.

    Cada acción:
      1. Persiste el cambio en fpoc.simpli_visits.
      2. Inserta audit row en fpoc.visit_interventions.
      3. Notifica por WhatsApp al driver + managers de la empresa + admins.
    """
    from routers.admin_day_notifications import _notify_supervisors
    from routers.notifications import send_whatsapp

    tid = req.tracking_id.strip()

    # 1) Cargar visita + driver/empresa.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT v.id, v.title, v.comuna, v.patente_falsa, v.status, "
            "       v.current_eta_cl, v.priority "
            "FROM fpoc.simpli_visits v "
            "WHERE CAST(v.id AS VARCHAR(32)) = ?",
            tid,
        )
        v = cur.fetchone()
    if v is None:
        raise HTTPException(404, f"Visita {tid} no existe")

    cliente = str(v[1] or "—")
    comuna = str(v[2] or "")
    patente = int(v[3]) if v[3] is not None else None
    cur_status = str(v[4] or "pending")
    cur_eta = str(v[5] or "")
    cur_priority = str(v[6] or "")
    cliente_label = f"{cliente}" + (f" ({comuna})" if comuna else "")

    if patente is None:
        raise HTTPException(409, f"Visita {tid} sin patente asignada")

    # 2) Resolver driver activo + empresa.
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT d.driver_id, d.name, d.phone_e164, d.notify_whatsapp, d.opted_in_at, "
            "       d.empresa_id, e.nombre AS empresa_nombre "
            "FROM fpoc.drivers d "
            "LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id "
            "WHERE d.vehicle_id = ? AND d.active = 1 "
            "ORDER BY d.opted_in_at DESC",
            patente,
        )
        d = cur.fetchone()

    driver_id = str(d[0]) if d and d[0] else None
    driver_name = str(d[1] or "—") if d else "—"
    driver_phone = str(d[2] or "").strip() if d else ""
    driver_notify = bool(d[3]) if d and d[3] is not None else False
    driver_optin = d[4] if d else None
    empresa_id = int(d[5]) if d and d[5] is not None else None
    empresa_nombre = str(d[6] or "—") if d else "—"

    # 3) Aplicar la acción + componer mensajes.
    before_value: str = ""
    after_value: str = ""
    body_driver: str = ""
    body_supervisors: str = ""
    triggered_by = f"admin_intervention_{req.action}"

    if req.action == "cancel":
        before_value = json.dumps({"status": cur_status})
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.simpli_visits "
                "SET status = 'cancelled', cancelled_at = SYSDATETIME() "
                "WHERE CAST(id AS VARCHAR(32)) = ?",
                tid,
            )
            cn.commit()
        after_value = json.dumps({"status": "cancelled"})
        body_driver = (
            f"⛔ *Falabella canceló* esta visita:\n"
            f"• Cliente: {cliente_label}\n"
            f"• Ya no la vas a visitar."
            + (f"\n• Motivo: {req.reason}" if req.reason else "")
        )
        body_supervisors = (
            f"⛔ *Admin canceló* folio TID:{tid}\n"
            f"• Empresa: {empresa_nombre}\n"
            f"• Driver: {driver_name} → {cliente_label}"
            + (f"\n• Razón: {req.reason}" if req.reason else "")
        )

    elif req.action == "reschedule":
        if not req.new_eta:
            raise HTTPException(400, "reschedule requiere new_eta (ISO datetime)")
        try:
            new_eta_dt = datetime.fromisoformat(req.new_eta.replace("Z", ""))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"new_eta inválido: {e}")
        before_value = json.dumps({"current_eta_cl": cur_eta})
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.simpli_visits "
                "SET current_eta_cl = ? "
                "WHERE CAST(id AS VARCHAR(32)) = ?",
                new_eta_dt, tid,
            )
            cn.commit()
        after_value = json.dumps({"current_eta_cl": req.new_eta})
        hora = new_eta_dt.strftime("%H:%M")
        body_driver = (
            f"📅 *Falabella reagendó* esta visita:\n"
            f"• Cliente: {cliente_label}\n"
            f"• Nueva ETA: *{hora}*"
            + (f"\n• Motivo: {req.reason}" if req.reason else "")
        )
        body_supervisors = (
            f"📅 *Admin reagendó* folio TID:{tid}\n"
            f"• Empresa: {empresa_nombre}\n"
            f"• Driver: {driver_name} → {cliente_label}\n"
            f"• Nueva ETA: {hora}"
            + (f"\n• Razón: {req.reason}" if req.reason else "")
        )

    elif req.action == "escalate_priority":
        new_pr = (req.priority or "HIGH").upper()
        before_value = json.dumps({"priority": cur_priority or None})
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.simpli_visits SET priority = ? "
                "WHERE CAST(id AS VARCHAR(32)) = ?",
                new_pr, tid,
            )
            cn.commit()
        after_value = json.dumps({"priority": new_pr})
        body_driver = (
            f"⭐ *Prioridad ALTA* (Falabella):\n"
            f"• Cliente: {cliente_label}\n"
            f"• Por favor priorizá esta entrega."
            + (f"\n• Motivo: {req.reason}" if req.reason else "")
        )
        body_supervisors = (
            f"⭐ *Admin elevó prioridad* TID:{tid}\n"
            f"• Empresa: {empresa_nombre}\n"
            f"• Driver: {driver_name} → {cliente_label}"
            + (f"\n• Razón: {req.reason}" if req.reason else "")
        )

    elif req.action == "override_motivo":
        if not req.new_motivo:
            raise HTTPException(400, "override_motivo requiere new_motivo")
        from routers.comments import MOTIVOS_CATALOGO
        if req.new_motivo not in MOTIVOS_CATALOGO:
            raise HTTPException(400, f"motivo inválido: {req.new_motivo!r}")
        # Buscar el último comment del folio.
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT TOP 1 comment_id, motivo FROM fpoc.visit_comments "
                "WHERE tracking_id = ? ORDER BY created_at DESC",
                tid,
            )
            c = cur.fetchone()
        if c is None:
            raise HTTPException(409, f"TID {tid} no tiene comments para override")
        comment_id = int(c[0])
        old_motivo = str(c[1] or "")
        before_value = json.dumps({"motivo": old_motivo, "comment_id": comment_id})
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc.visit_comments SET motivo = ? WHERE comment_id = ?",
                req.new_motivo, comment_id,
            )
            cn.commit()
        after_value = json.dumps({"motivo": req.new_motivo, "comment_id": comment_id})
        body_driver = (
            f"🤖 *Coordinador revisó tu motivo*:\n"
            f"• Cliente: {cliente_label}\n"
            f"• Cambio: {old_motivo} → *{req.new_motivo}*"
            + (f"\n• Razón: {req.reason}" if req.reason else "")
        )
        body_supervisors = (
            f"📝 *Admin corrigió motivo* TID:{tid}\n"
            f"• Empresa: {empresa_nombre}\n"
            f"• Driver: {driver_name} → {cliente_label}\n"
            f"• {old_motivo} → {req.new_motivo}"
            + (f"\n• Razón: {req.reason}" if req.reason else "")
        )

    # 4) Audit row.
    intervention_id: Optional[int] = None
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                INSERT INTO fpoc.visit_interventions
                  (tracking_id, action, admin_user_id, admin_name,
                   before_value, after_value, reason)
                OUTPUT INSERTED.intervention_id
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tid, req.action, user.user_id, user.display_name,
                before_value, after_value, req.reason,
            )
            r = cur.fetchone()
            intervention_id = int(r[0]) if r else None
            cn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[intervention] audit insert fallo TID={tid}: {e}")

    # 5) Notif driver (freeform — requiere ventana 24h).
    driver_notified = False
    if (
        driver_phone.startswith("+")
        and driver_notify
        and driver_optin is not None
        and body_driver
    ):
        try:
            send_whatsapp(
                body=body_driver,
                targets=[(None, driver_phone)],
                subject=f"Intervención · TID:{tid}",
                tracking_id=tid,
                triggered_by=triggered_by,
            )
            driver_notified = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[intervention] driver {driver_id} fallo: {e}")

    # 6) Notif supervisors (managers de la empresa + admins Falabella).
    manager_n, admin_n = 0, 0
    if body_supervisors:
        try:
            manager_n, admin_n = _notify_supervisors(
                empresa_id, body_supervisors,
                subject=f"Intervención · {empresa_nombre}",
                tracking_id=tid,
                triggered_by=triggered_by,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[intervention] supervisors fallo TID={tid}: {e}")

    detail = (
        f"action={req.action} driver={driver_notified} mgrs={manager_n} admins={admin_n}"
    )
    logger.info(
        f"[intervention] TID={tid} by user_id={user.user_id} ({user.email}) {detail}"
    )

    return VisitInterventionResponse(
        intervention_id=intervention_id,
        tracking_id=tid,
        action=req.action,
        driver_notified=driver_notified,
        manager_notified_count=manager_n,
        admin_notified_count=admin_n,
        before_value=before_value,
        after_value=after_value,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Listing endpoint (audit view en dashboard admin)
# ---------------------------------------------------------------------------

@router.get("/visit-interventions")
def list_interventions(
    tracking_id: Optional[str] = None,
    limit: int = 50,
    user: CurrentUser = Depends(_require_admin_or_ops),
) -> list[dict]:
    """Listado de intervenciones (audit). Si tracking_id viene, filtra."""
    sql = (
        "SELECT TOP (?) intervention_id, tracking_id, action, admin_user_id, "
        "       admin_name, before_value, after_value, reason, created_at "
        "FROM fpoc.visit_interventions "
    )
    params: list = [max(1, min(limit, 200))]
    if tracking_id:
        sql += "WHERE tracking_id = ? "
        params.append(tracking_id.strip())
    sql += "ORDER BY created_at DESC"
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(sql, *params)
        rows = cur.fetchall()
    return [
        {
            "intervention_id": int(r[0]),
            "tracking_id": str(r[1]),
            "action": str(r[2]),
            "admin_user_id": int(r[3]) if r[3] is not None else None,
            "admin_name": str(r[4] or ""),
            "before_value": str(r[5] or ""),
            "after_value": str(r[6] or ""),
            "reason": str(r[7] or ""),
            "created_at": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]),
        }
        for r in rows
    ]

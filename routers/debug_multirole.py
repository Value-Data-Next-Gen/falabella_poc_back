"""Endpoint debug: dispara 3 mensajes (driver, jefe, admin) al MISMO phone.

Útil para que un mismo usuario (Gonzalo) reciba las 3 perspectivas distintas
en su WhatsApp y vea qué le llegaría a cada rol en producción.

Bypasea el dedupe-por-phone del dispatcher normal y los filtros opt-in que
ya validaron antes (este endpoint asume que el phone está opted-in en los
3 roles porque eso es responsabilidad del setup).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn


router = APIRouter(prefix="/api/admin/debug", tags=["debug-multirole"])


class MultiRoleEventRequest(BaseModel):
    tracking_id: str
    event: str = "complete"  # 'complete' | 'delay' | 'reschedule' | 'comment'
    target_phone: str        # phone único que recibe las 3 perspectivas
    motivo: Optional[str] = None
    new_eta: Optional[str] = None


class MultiRoleEventResponse(BaseModel):
    tracking_id: str
    event: str
    target_phone: str
    sent_count: int
    perspectives_sent: list[str]
    detail: str


@router.post("/multirole-event", response_model=MultiRoleEventResponse)
def trigger_multirole_event(
    req: MultiRoleEventRequest,
    user: CurrentUser = Depends(current_user),
) -> MultiRoleEventResponse:
    """Envía 3 mensajes (driver / jefe / admin) al mismo phone con bodies
    distintos para cada perspectiva."""
    if not user.is_falabella:
        raise HTTPException(403, "Solo admin/ops")

    from routers.notifications import send_whatsapp

    tid = req.tracking_id.strip()
    phone = req.target_phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "target_phone debe estar en E.164 (+...)")

    # 1) Resolver datos de la visita
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT v.title, v.comuna, v.patente_falsa, v.status, v.planned_date "
            "FROM fpoc.simpli_visits v "
            "WHERE CAST(v.id AS VARCHAR(32)) = ?",
            tid,
        )
        v = cur.fetchone()
    if v is None:
        raise HTTPException(404, f"Visita {tid} no existe")

    cliente = str(v[0] or "—")
    comuna = str(v[1] or "")
    patente = int(v[2]) if v[2] is not None else None
    cliente_label = f"{cliente}" + (f" ({comuna})" if comuna else "")
    hora = datetime.now().strftime("%H:%M")

    # 2) Resolver driver/empresa info
    driver_name = "Driver"
    empresa_nombre = "Empresa"
    if patente is not None:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT d.name, e.nombre FROM fpoc.drivers d "
                "LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id "
                "WHERE d.vehicle_id = ? AND d.active = 1",
                patente,
            )
            r = cur.fetchone()
            if r:
                driver_name = str(r[0] or driver_name)
                empresa_nombre = str(r[1] or empresa_nombre)

    perspectives_sent = []
    sent_count = 0

    def _send(body: str, role_label: str, subject: str) -> None:
        nonlocal sent_count
        try:
            send_whatsapp(
                body=body,
                targets=[(None, phone)],
                subject=subject,
                tracking_id=tid,
                triggered_by=f"debug_multirole_{role_label}",
            )
            sent_count += 1
            perspectives_sent.append(role_label)
            logger.info(f"[debug-multirole] sent {role_label} body to {phone[:6]}...")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[debug-multirole] {role_label} fallo: {e}")

    # 3) Bodies según evento + rol
    if req.event == "complete":
        _send(
            body=(
                f"🚚 *[DRIVER]* ✅ Entrega OK\n"
                f"• Cliente: {cliente_label}\n"
                f"• Hora: {hora}\n"
                f"Mandá 'menu' para tu ruta restante."
            ),
            role_label="driver",
            subject=f"[DRIVER] Entrega OK · {cliente}",
        )
        _send(
            body=(
                f"📊 *[JEFE Transporte]* Entrega completada\n"
                f"• Driver: {driver_name}\n"
                f"• Cliente: {cliente_label}\n"
                f"• Hora: {hora}\n"
                f"• Empresa: {empresa_nombre}\n"
                f"Andá al panel para ver KPI del día."
            ),
            role_label="jefe",
            subject=f"[JEFE] Entrega · {empresa_nombre} · {cliente}",
        )
        _send(
            body=(
                f"🏢 *[ADMIN Falabella]* Entrega registrada\n"
                f"• Empresa: *{empresa_nombre}*\n"
                f"• Driver: {driver_name}\n"
                f"• Cliente: {cliente_label} · {hora}\n"
                f"Dashboard global: panel admin → KPIs día."
            ),
            role_label="admin",
            subject=f"[ADMIN] Entrega · cross-empresa · {cliente}",
        )

    elif req.event == "delay":
        eta = req.new_eta or "ETA + 30min"
        motivo = req.motivo or "Atraso por tráfico"
        _send(
            body=(
                f"🚚 *[DRIVER]* ⚠️ Atraso ETA\n"
                f"• Cliente: {cliente_label}\n"
                f"• Nueva ETA: {eta}\n"
                f"• Motivo: {motivo}\n"
                f"Respondé con la causa o '1' para confirmar."
            ),
            role_label="driver",
            subject=f"[DRIVER] Atraso ETA · {cliente}",
        )
        _send(
            body=(
                f"📊 *[JEFE Transporte]* Driver atrasado\n"
                f"• Driver: {driver_name}\n"
                f"• Cliente: {cliente_label}\n"
                f"• ETA estimada: {eta}\n"
                f"• Motivo reportado: {motivo}\n"
                f"Evalúa reasignar ruta si aplica."
            ),
            role_label="jefe",
            subject=f"[JEFE] Atraso · {driver_name}",
        )
        _send(
            body=(
                f"🏢 *[ADMIN Falabella]* Alerta atraso cross-empresa\n"
                f"• Empresa: *{empresa_nombre}*\n"
                f"• Driver: {driver_name} → {cliente_label}\n"
                f"• ETA actualizada: {eta}\n"
                f"Considerá intervenir folio si supera SLA."
            ),
            role_label="admin",
            subject=f"[ADMIN] Atraso · {empresa_nombre}",
        )

    elif req.event == "reschedule":
        new_eta_str = req.new_eta or "mañana 10:00"
        _send(
            body=(
                f"🚚 *[DRIVER]* 📅 Falabella reagendó\n"
                f"• Cliente: {cliente_label}\n"
                f"• Nueva ETA: *{new_eta_str}*\n"
                f"Ya no la visites hoy."
            ),
            role_label="driver",
            subject=f"[DRIVER] Reagendado · {cliente}",
        )
        _send(
            body=(
                f"📊 *[JEFE Transporte]* Folio reagendado por Falabella\n"
                f"• Driver afectado: {driver_name}\n"
                f"• Cliente: {cliente_label}\n"
                f"• Nueva ETA: {new_eta_str}\n"
                f"Ajustá dotación del día siguiente."
            ),
            role_label="jefe",
            subject=f"[JEFE] Reagendado · {empresa_nombre}",
        )
        _send(
            body=(
                f"🏢 *[ADMIN Falabella]* Intervención aplicada\n"
                f"• Tipo: reschedule\n"
                f"• Empresa: {empresa_nombre}\n"
                f"• Folio: {cliente_label} → {new_eta_str}\n"
                f"Audit row registrado."
            ),
            role_label="admin",
            subject=f"[ADMIN] Intervención · {cliente}",
        )

    elif req.event == "comment":
        motivo = req.motivo or "SIN MORADORES"
        _send(
            body=(
                f"🚚 *[DRIVER]* Tu motivo fue registrado\n"
                f"• Cliente: {cliente_label}\n"
                f"• Motivo: *{motivo}*\n"
                f"La IA está revisando coincidencia con el comentario."
            ),
            role_label="driver",
            subject=f"[DRIVER] Motivo · {motivo}",
        )
        _send(
            body=(
                f"📊 *[JEFE Transporte]* Driver reportó motivo\n"
                f"• Driver: {driver_name}\n"
                f"• Cliente: {cliente_label}\n"
                f"• Motivo: *{motivo}*\n"
                f"Si es crítico, contactá al driver."
            ),
            role_label="jefe",
            subject=f"[JEFE] Motivo reportado · {motivo}",
        )
        _send(
            body=(
                f"🏢 *[ADMIN Falabella]* Motivo crítico detectado\n"
                f"• Empresa: *{empresa_nombre}*\n"
                f"• Driver: {driver_name}\n"
                f"• Motivo: *{motivo}*\n"
                f"Cliente: {cliente_label}\n"
                f"Revisar correcciones IA pending si aplica."
            ),
            role_label="admin",
            subject=f"[ADMIN] Motivo crítico · {empresa_nombre}",
        )
    else:
        raise HTTPException(400, f"event '{req.event}' no soportado. Usa: complete/delay/reschedule/comment")

    detail = (
        f"Enviados {sent_count}/3 mensajes a {phone[:6]}... "
        f"({', '.join(perspectives_sent)}) para evento '{req.event}'"
    )
    logger.info(f"[debug-multirole] {detail}")

    return MultiRoleEventResponse(
        tracking_id=tid,
        event=req.event,
        target_phone=phone,
        sent_count=sent_count,
        perspectives_sent=perspectives_sent,
        detail=detail,
    )

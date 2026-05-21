"""Sprint 4.A2 — Validación LLM automática del motivo reportado por el chofer.

Cuando se persiste un comentario alertable, llamamos al classifier LLM. Si
sugiere un motivo distinto al reportado y la confianza es alta/media, creamos
una fila en `fpoc_motivo_corrections` (status='pending') y emitimos un evento
`motivo_correction_suggested`.

Si el driver del vehículo tiene `notify_whatsapp=1 AND opted_in_at NOT NULL`,
también se le envía un WhatsApp pidiendo confirmación. En este POC todos los
drivers están con notify_whatsapp=0, así que el envío queda como dry_run.

Endpoints (admin/ops):
  GET  /api/motivo-corrections?status=pending&limit=50
  POST /api/motivo-corrections/{id}/accept
  POST /api/motivo-corrections/{id}/reject
  POST /api/motivo-corrections/{id}/no-action
  POST /api/motivo-corrections/{id}/renotify-driver
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from routers.comments import (
    MOTIVOS_CATALOGO,
    _build_alert_whatsapp_body,
    _resolve_alert_config,
    _visit_meta,
    _visit_region,
)
from core.db import get_conn
from core.events import EVENTS
from routers.motivo_classifier import _classify_llm
from core.state import STATE


router = APIRouter(prefix="/api/motivo-corrections", tags=["motivo-corrections"])


# =============================================================================
# Schemas
# =============================================================================
class MotivoCorrectionOut(BaseModel):
    correction_id: int
    comment_id: int
    tracking_id: str
    motivo_reportado: str
    motivo_sugerido: str
    confianza: str
    razonamiento: str
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    status: str
    decided_by_user_id: Optional[int] = None
    decided_at: Optional[str] = None
    notified_driver_at: Optional[str] = None
    created_at: str
    # context
    vehicle_name: Optional[str] = None
    empresa_nombre: Optional[str] = None
    comentario: Optional[str] = None


# =============================================================================
# Helpers
# =============================================================================
def _row_to_correction(r) -> MotivoCorrectionOut:
    def _iso(v):
        if v is None: return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
    return MotivoCorrectionOut(
        correction_id=int(r.correction_id),
        comment_id=int(r.comment_id),
        tracking_id=r.tracking_id,
        motivo_reportado=r.motivo_reportado,
        motivo_sugerido=r.motivo_sugerido,
        confianza=r.confianza,
        razonamiento=r.razonamiento,
        driver_id=r.driver_id,
        driver_name=getattr(r, "driver_name", None),
        status=r.status,
        decided_by_user_id=int(r.decided_by_user_id) if r.decided_by_user_id is not None else None,
        decided_at=_iso(r.decided_at),
        notified_driver_at=_iso(r.notified_driver_at),
        created_at=_iso(r.created_at) or "",
        vehicle_name=getattr(r, "vehicle_name", None),
        empresa_nombre=getattr(r, "empresa_nombre", None),
        comentario=getattr(r, "comentario", None),
    )


def _build_driver_correction_body(
    *, tracking_id: str, motivo_reportado: str, motivo_sugerido: str,
    confianza: str, razonamiento: str,
) -> str:
    return (
        "🤖 Revisión IA — Falabella ValueData\n"
        f"Tracking: {tracking_id}\n\n"
        f"Reportaste: *{motivo_reportado}*\n"
        f"IA sugiere: *{motivo_sugerido}* (confianza {confianza})\n\n"
        f"Motivo IA: {razonamiento}\n\n"
        "¿Confirmás el motivo correcto? Responde el manager o usá la app."
    )


def _send_to_driver_if_optedin(
    *, driver_id: Optional[str], tracking_id: str, body: str,
    motivo_reportado: Optional[str] = None,
    motivo_sugerido: Optional[str] = None,
) -> Optional[str]:
    """Devuelve ISO timestamp si se intentó enviar (queued/dry_run/sent), None si no.
    Toda la info se loguea en `fpoc_notifications_log` con triggered_by='motivo_correction'."""
    if not driver_id:
        return None
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT phone_e164, notify_whatsapp, opted_in_at
                FROM fpoc_drivers
                WHERE driver_id = ? AND active = 1
                """,
                driver_id,
            )
            r = cur.fetchone()
        if r is None:
            return None
        if not (bool(r.notify_whatsapp) and r.opted_in_at and r.phone_e164):
            return None
        # Driver válido: enviar (Twilio decide dry_run / send).
        # Template Meta-approved vd_revision_ia_v2 (2 vars: motivo_reportado, motivo_sugerido).
        # Fallback freeform si el template falla — body legacy preserva razonamiento.
        from routers.notifications import send_whatsapp
        from routers.comments import _sanitize_template_var as _sanvar
        from core.twilio_templates import revision_ia_sid
        content_sid = revision_ia_sid()
        used_template = False
        if content_sid:
            try:
                send_whatsapp(
                    content_sid=content_sid,
                    content_variables={
                        "1": _sanvar(motivo_reportado) or "—",
                        "2": _sanvar(motivo_sugerido) or "—",
                    },
                    targets=[(None, r.phone_e164)],
                    subject=f"Revisión IA · {tracking_id}",
                    tracking_id=tracking_id,
                    triggered_by="motivo_correction",
                )
                used_template = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[motivo-corr] template vd_revision_ia_v2 falló (driver), fallback freeform: {e}")
        if not used_template:
            send_whatsapp(
                body=body,
                targets=[(None, r.phone_e164)],
                subject=f"Revisión IA · {tracking_id}",
                tracking_id=tracking_id,
                triggered_by="motivo_correction",
            )
        # Asociar driver_id en log (best-effort)
        try:
            with get_conn() as cn2:
                cur2 = cn2.cursor()
                # LIMIT en subquery no se reescribe a TOP por el rewriter
                # (`_rewrite_sql_for_mssql` solo toca LIMIT al tail outer).
                # Usar MAX(notification_id) para semántica "última fila"
                # portable a Azure SQL + SQLite.
                cur2.execute(
                    """
                    UPDATE fpoc_notifications_log
                    SET driver_id = ?
                    WHERE notification_id = (
                        SELECT MAX(notification_id) FROM fpoc_notifications_log
                        WHERE tracking_id = ? AND to_number = ?
                          AND user_id IS NULL AND contact_id IS NULL
                          AND driver_id IS NULL
                    )
                    """,
                    driver_id, tracking_id, r.phone_e164,
                )
                cn2.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[motivo-corr] backfill driver_id falló: {e}")
        return datetime.utcnow().isoformat()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[motivo-corr] send_to_driver falló: {e}")
        return None


def maybe_create_correction_from_comment(
    *,
    comment_id: int,
    tracking_id: str,
    motivo_reportado: str,
    comentario: str,
    empresa_id: Optional[int],
    user_display_name: str,
) -> Optional[int]:
    """Hook llamado desde comments._persist_and_dispatch_comment después de
    insertar y dispatch del comentario.

    Devuelve correction_id si se creó una corrección, None si no aplica.
    Tolerante a fallos (best-effort): cualquier excepción se loguea y devuelve None.
    """
    try:
        result = _classify_llm(comentario, empresa_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[motivo-corr] classifier falló: {e}")
        return None

    if not result or result.get("fallback"):
        return None

    motivo_sugerido = result["motivo"]
    if motivo_sugerido == motivo_reportado:
        return None
    confianza = (result.get("confianza") or "").lower()
    if confianza not in ("alta", "media"):
        return None
    razonamiento = (result.get("razonamiento") or "")[:400]

    # Driver + region del vehículo (si está disponible)
    meta = _visit_meta(tracking_id) or {}
    driver_id = meta.get("driver_id")
    from routers.comments import _visit_region as _viz_region
    region = _viz_region(meta.get("latitude"), meta.get("longitude"))

    correction_id: Optional[int] = None
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                INSERT INTO fpoc_motivo_corrections
                  (comment_id, tracking_id, motivo_reportado, motivo_sugerido,
                   confianza, razonamiento, driver_id, status, region)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                comment_id, tracking_id, motivo_reportado, motivo_sugerido,
                confianza, razonamiento, driver_id, region,
            )
            cn.commit()
            cur.execute("SELECT last_insert_rowid() AS id")
            correction_id = int(cur.fetchone().id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[motivo-corr] insert falló: {e}")
        return None

    # Evento al stream
    EVENTS.emit("motivo_correction_suggested", STATE.sim_clock or datetime.utcnow(), {
        "tracking_id": tracking_id,
        "comment_id": comment_id,
        "correction_id": correction_id,
        "motivo_reportado": motivo_reportado,
        "motivo_sugerido": motivo_sugerido,
        "confianza": confianza,
        "razonamiento": razonamiento,
        "vehicle_name": meta.get("vehicle_name"),
        "title": meta.get("title"),
    })

    # WhatsApp al driver (sólo si tiene opt-in; en POC nadie lo tiene → no se envía)
    if os.environ.get("ENABLE_AUTO_NOTIFY", "false").lower() == "true":
        body = _build_driver_correction_body(
            tracking_id=tracking_id,
            motivo_reportado=motivo_reportado,
            motivo_sugerido=motivo_sugerido,
            confianza=confianza,
            razonamiento=razonamiento,
        )
        ts = _send_to_driver_if_optedin(
            driver_id=driver_id, tracking_id=tracking_id, body=body,
            motivo_reportado=motivo_reportado, motivo_sugerido=motivo_sugerido,
        )
        if ts:
            try:
                with get_conn() as cn:
                    cur = cn.cursor()
                    cur.execute(
                        "UPDATE fpoc_motivo_corrections SET notified_driver_at = ? WHERE correction_id = ?",
                        ts, correction_id,
                    )
                    cn.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[motivo-corr] mark notified falló: {e}")

        # Aviso al manager / contactos de la empresa con el subject distinto
        try:
            from routers.comments import _resolve_alert_targets, _backfill_contact_id_in_log
            from routers.notifications import send_whatsapp
            _, severity = _resolve_alert_config(motivo_sugerido, empresa_id)
            visit_region = _visit_region(meta.get("latitude"), meta.get("longitude"))
            targets, contact_ids_by_phone = _resolve_alert_targets(
                empresa_id=empresa_id, severity=severity,
                motivo=motivo_sugerido, visit_region=visit_region,
            )
            if targets:
                manager_subject = f"Revisión IA · {motivo_reportado} → {motivo_sugerido}"
                # Template Meta-approved vd_revision_ia_v2 (2 vars).
                # El template body no distingue audiencia: las mismas 2 vars
                # sirven para driver y para manager.
                from routers.comments import _sanitize_template_var as _sanvar
                from core.twilio_templates import revision_ia_sid
                content_sid = revision_ia_sid()
                used_template = False
                if content_sid:
                    try:
                        send_whatsapp(
                            content_sid=content_sid,
                            content_variables={
                                "1": _sanvar(motivo_reportado) or "—",
                                "2": _sanvar(motivo_sugerido) or "—",
                            },
                            targets=targets,
                            subject=manager_subject,
                            tracking_id=tracking_id,
                            triggered_by="motivo_correction",
                        )
                        used_template = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[motivo-corr] template vd_revision_ia_v2 falló (manager), fallback freeform: {e}")
                if not used_template:
                    manager_body = (
                        "🤖 *Revisión IA* — Falabella ValueData\n"
                        f"*Tracking:* {tracking_id}\n"
                        f"*Reportado:* {motivo_reportado}\n"
                        f"*Sugerido:* {motivo_sugerido} (confianza {confianza})\n"
                        f"*Razonamiento:* {razonamiento}\n\n"
                        f"*Cliente:* {meta.get('title') or '—'}\n"
                        f"*Vehículo:* {meta.get('vehicle_name') or '—'}\n"
                        f"*Reportado por:* {user_display_name}\n\n"
                        "Revisar en panel: Seguimiento IA → Correcciones de motivo."
                    )
                    send_whatsapp(
                        body=manager_body, targets=targets,
                        subject=manager_subject,
                        tracking_id=tracking_id,
                        triggered_by="motivo_correction",
                    )
                _backfill_contact_id_in_log(
                    tracking_id=tracking_id,
                    contact_ids_by_phone=contact_ids_by_phone,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[motivo-corr] manager notify falló: {e}")

    return correction_id


# =============================================================================
# Endpoints
# =============================================================================
SELECT_BASE = """
    SELECT mc.correction_id, mc.comment_id, mc.tracking_id,
           mc.motivo_reportado, mc.motivo_sugerido,
           mc.confianza, mc.razonamiento, mc.driver_id,
           mc.status, mc.decided_by_user_id, mc.decided_at,
           mc.notified_driver_at, mc.created_at,
           c.comentario AS comentario, c.empresa_id AS empresa_id,
           c.vehicle_id AS vehicle_id,
           d.name AS driver_name,
           v.name AS vehicle_name,
           e.nombre AS empresa_nombre
    FROM fpoc_motivo_corrections mc
    LEFT JOIN fpoc_visit_comments c ON c.comment_id = mc.comment_id
    LEFT JOIN fpoc_drivers d ON d.driver_id = mc.driver_id
    LEFT JOIN fpoc_vehicles v ON v.vehicle_id = c.vehicle_id
    LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = c.empresa_id
"""


@router.get("", response_model=list[MotivoCorrectionOut])
def list_corrections(
    status: Optional[str] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=500),
    user: CurrentUser = Depends(current_user),
) -> list[MotivoCorrectionOut]:
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops")
    where = ""
    params: list = []
    if status and status != "all":
        if status not in ("pending", "accepted", "rejected", "no_action"):
            raise HTTPException(400, f"status inválido: {status}")
        where = " WHERE mc.status = ?"
        params.append(status)
    params.append(limit)
    sql = SELECT_BASE + where + " ORDER BY mc.created_at DESC LIMIT ?"
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(sql, *params)
        return [_row_to_correction(r) for r in cur.fetchall()]


def _fetch_correction(correction_id: int) -> MotivoCorrectionOut:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(SELECT_BASE + " WHERE mc.correction_id = ?", correction_id)
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, "correction no encontrada")
        return _row_to_correction(r)


def _update_decision(correction_id: int, status: str, user_id: int) -> None:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            UPDATE fpoc_motivo_corrections
            SET status = ?, decided_by_user_id = ?, decided_at = CURRENT_TIMESTAMP
            WHERE correction_id = ?
            """,
            status, user_id, correction_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "correction no encontrada")
        cn.commit()


@router.post("/{correction_id}/accept", response_model=MotivoCorrectionOut)
def accept_correction(correction_id: int, user: CurrentUser = Depends(current_user)) -> MotivoCorrectionOut:
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops")

    # 1) Obtener correction + comment_id + motivos + tracking_id + comentario
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT c.correction_id, c.comment_id, c.tracking_id, "
            "       c.motivo_reportado, c.motivo_sugerido, c.status, "
            "       v.comentario, v.empresa_id "
            "FROM fpoc_motivo_corrections c "
            "LEFT JOIN fpoc_visit_comments v ON v.comment_id = c.comment_id "
            "WHERE c.correction_id = ?",
            correction_id,
        )
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, "correction no encontrada")
        if r.status != "pending":
            raise HTTPException(409, f"correction ya está en estado {r.status}")
        # 2) Aplicar el motivo sugerido al comentario original
        cur.execute(
            "UPDATE fpoc_visit_comments SET motivo = ? WHERE comment_id = ?",
            r.motivo_sugerido, r.comment_id,
        )
        cn.commit()

    # 3) Marcar la correction como accepted
    _update_decision(correction_id, "accepted", user.user_id)

    # 4) Re-disparar alerta si la severity escaló
    redispatch_info = {"redispatched": False}
    try:
        from routers.comments import _resolve_alert_config, dispatch_comment_alert, severity_rank
        old_alertable, old_sev = _resolve_alert_config(r.motivo_reportado, r.empresa_id)
        new_alertable, new_sev = _resolve_alert_config(r.motivo_sugerido, r.empresa_id)
        escalated = (new_alertable and not old_alertable) or (
            new_alertable and severity_rank(new_sev) > severity_rank(old_sev)
        )
        if escalated:
            result = dispatch_comment_alert(
                tracking_id=r.tracking_id,
                motivo=r.motivo_sugerido,
                comentario=r.comentario or "",
                user_display_name=f"IA aceptada por {user.display_name}",
                triggered_by="motivo_correction_redispatch",
                extra_subject="🤖 IA reclasificó",
            )
            redispatch_info = {
                "redispatched": True,
                "old_severity": old_sev, "new_severity": new_sev,
                "sent": result.get("sent", 0),
            }
    except Exception as e:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"[corrections.accept] re-dispatch falló: {e}")

    # 5) Emitir evento informativo (UI lo refleja)
    EVENTS.emit("motivo_correction_decided", STATE.sim_clock or datetime.utcnow(), {
        "correction_id": correction_id,
        "decision": "accepted",
        "motivo_aplicado": r.motivo_sugerido,
        "decided_by": user.display_name,
        **redispatch_info,
    })

    return _fetch_correction(correction_id)


@router.post("/{correction_id}/reject", response_model=MotivoCorrectionOut)
def reject_correction(correction_id: int, user: CurrentUser = Depends(current_user)) -> MotivoCorrectionOut:
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops")
    _update_decision(correction_id, "rejected", user.user_id)
    EVENTS.emit("motivo_correction_decided", STATE.sim_clock or datetime.utcnow(), {
        "correction_id": correction_id,
        "decision": "rejected",
        "decided_by": user.display_name,
    })
    return _fetch_correction(correction_id)


@router.post("/{correction_id}/no-action", response_model=MotivoCorrectionOut)
def no_action_correction(correction_id: int, user: CurrentUser = Depends(current_user)) -> MotivoCorrectionOut:
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops")
    _update_decision(correction_id, "no_action", user.user_id)
    return _fetch_correction(correction_id)


@router.post("/{correction_id}/renotify-driver", response_model=MotivoCorrectionOut)
def renotify_driver(correction_id: int, user: CurrentUser = Depends(current_user)) -> MotivoCorrectionOut:
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT tracking_id, driver_id, motivo_reportado, motivo_sugerido, confianza, razonamiento "
            "FROM fpoc_motivo_corrections WHERE correction_id = ?",
            correction_id,
        )
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, "correction no encontrada")

    body = _build_driver_correction_body(
        tracking_id=r.tracking_id,
        motivo_reportado=r.motivo_reportado,
        motivo_sugerido=r.motivo_sugerido,
        confianza=r.confianza,
        razonamiento=r.razonamiento,
    )
    ts: Optional[str] = None
    if os.environ.get("ENABLE_AUTO_NOTIFY", "false").lower() == "true":
        ts = _send_to_driver_if_optedin(
            driver_id=r.driver_id, tracking_id=r.tracking_id, body=body,
            motivo_reportado=r.motivo_reportado, motivo_sugerido=r.motivo_sugerido,
        )
    if ts:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "UPDATE fpoc_motivo_corrections SET notified_driver_at = ? WHERE correction_id = ?",
                ts, correction_id,
            )
            cn.commit()
    return _fetch_correction(correction_id)

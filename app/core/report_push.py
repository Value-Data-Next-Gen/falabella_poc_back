"""End-of-day report push (CR-3b).

When a día is CERRADO, WhatsApp a one-line summary (visitas, éxito,
puntualidad) to the empresa's contactos and usuarios via the Meta-approved
REPORTE_DIA template. Drivers are excluded — this is a management summary.

Best-effort: failures never block the día-close transition. If the template is
not yet approved by WhatsApp, Twilio rejects the send and we simply log it.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.twilio_templates import reporte_dia_sid
from app.core.whatsapp import send_whatsapp
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa


def _pct(value: float | None) -> str:
    return f"{value}%" if value is not None else "-"


async def push_dia_report(db: AsyncSession, dia: DiaOperativo) -> int:
    """Compose + send the día's report to the empresa's contactos/usuarios.

    Returns the number of messages sent. No-op (0) if no template SID is
    configured.
    """
    sid = reporte_dia_sid()
    if not sid:
        return 0

    # Lazy imports: report aggregation lives in the API layer; the recipient
    # loaders live in alert_dispatcher. Importing at module top would invert
    # core→api layering / risk a cycle.
    from app.api.v1.reports import _estado_counts, _on_time_overall, _outcome
    from app.core.alert_dispatcher import (
        _is_mssql,
        _load_recipients_fallback,
        _load_recipients_mssql,
    )

    grace = settings.alerts_grace_min
    counts = _outcome(await _estado_counts(db, [dia.dia_id]))
    on_time = await _on_time_overall(db, [dia.dia_id], grace)
    empresa_nombre = await db.scalar(
        select(Empresa.nombre).where(Empresa.empresa_id == dia.empresa_id)
    ) or f"Empresa {dia.empresa_id}"

    content_vars = {
        "1": str(empresa_nombre)[:60],
        "2": dia.fecha.isoformat(),
        "3": str(counts.visitas),
        "4": str(counts.entregado),
        "5": _pct(counts.success_pct),
        "6": _pct(on_time.on_time_pct),
    }

    recipients = (
        await _load_recipients_mssql(db, dia.empresa_id)
        if _is_mssql(db)
        else await _load_recipients_fallback(db, dia.empresa_id)
    )
    # Management summary → contactos + usuarios only (not drivers).
    targets = [r for r in recipients if r.recipient_type in ("contacto", "user") and r.phone_e164]

    sent = 0
    for r in targets:
        if await send_whatsapp(to=r.phone_e164, content_sid=sid, content_variables=content_vars):
            sent += 1
    logger.info(f"[report_push] dia {dia.dia_id} report -> {sent}/{len(targets)} sent")
    return sent

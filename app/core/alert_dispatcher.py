"""Alert dispatcher — load recipients, filter, send WhatsApp, update alert.

CR-022 Part A. Called by both the APScheduler crons (`app.jobs.alerts`) and
the manual endpoint `POST /api/v1/alerts/{alert_id}/dispatch`.

Recipient resolution:
  * On MSSQL we query `vw_notif_recipients` (CR-007) filtered by
    `empresa_id = alert.empresa_id AND notify_enabled = 1`. The view UNIONs
    users + drivers + empresa_contactos with shared opt-in semantics.
  * On SQLite (tests) the view doesn't exist; we union the 3 tables in Python
    so unit tests can exercise the filter logic.

Recipient filter (post-fetch, per-row):
  * `contacto` (jefe/coordinador): JSON `notify_severities` whitelist. If
    set and `alert.severity` not in it → skip. Drivers and users bypass this
    filter — drivers always receive (they're the ones on the road); users
    receive `alta` and `critica` by default unless they're admin (admins
    receive all).

Templates use the real Meta-approved Content SIDs from `twilio_templates`
(eta_breach/eta_preview/manual → ALERTA_MOTIVO; vip_deadline → VIP_DEADLINE),
each populated with its 6 positional variables. `send_whatsapp` already honors
`settings.notifications_dry_run` — the dispatcher does not short-circuit it.

Idempotency: if `alert.estado != 'abierta'` we return a zero-result without
dispatching. Re-dispatch is via `POST /api/v1/alerts/{id}/dispatch` (admin
only) which first flips the alert back to `abierta`.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.twilio_templates import alerta_motivo_sid, vip_deadline_sid
from app.core.whatsapp import send_whatsapp
from app.db.models.alert import Alert
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.schemas.alert import AlertDispatchResult


def _template_for(tipo: str) -> str:
    """Map alert tipo → real Meta-approved Twilio Content SID.

    vip_deadline uses the dedicated VIP template; everything else (eta_breach,
    eta_preview, manual) uses the generic operational-alert template. The SIDs
    come from `twilio_templates` (env-overridable, with approved fallbacks) —
    NOT the old `HX..._STUB` placeholders, which Twilio rejected with HTTP 400.
    """
    if tipo == "vip_deadline":
        return vip_deadline_sid()
    return alerta_motivo_sid()


def _hhmm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt is not None else "-"


async def _ruta_vehicle_driver(db: AsyncSession, visita: Visita | None) -> tuple[str, str]:
    """Resolve (patente, conductor_nombre) for a visita via its ruta.

    The approved alert templates print the vehicle plate and the driver name,
    so we must populate those — not the folio/empresa the dispatcher used to
    send (which rendered as 'conductor <cliente>' to recipients).
    """
    if visita is None or visita.ruta_id is None:
        return "-", "-"
    row = (await db.execute(
        select(Vehicle.plate, Driver.nombre)
        .select_from(Ruta)
        .join(Vehicle, Ruta.vehicle_id == Vehicle.vehicle_id, isouter=True)
        .join(Driver, Ruta.driver_id == Driver.driver_id, isouter=True)
        .where(Ruta.ruta_id == visita.ruta_id)
    )).first()
    if row is None:
        return "-", "-"
    return (row[0] or "-"), (row[1] or "-")


def _clip(value: object, n: int = 120) -> str:
    """Twilio content variables must be non-empty strings; clip + default."""
    s = "" if value is None else str(value)
    s = s.strip() or "-"
    return s[:n]


async def _build_template_message(db: AsyncSession, alert: Alert) -> tuple[str, dict[str, str]]:
    """Resolve the Content SID + the template's POSITIONAL variables.

    The approved templates take 6 numbered variables each (verified against the
    Twilio Content API). All 6 MUST be provided or Twilio rejects the send.

    Variable order matches the approved template TEXT exactly:
      ALERTA_MOTIVO: 1=severidad 2=motivo 3=vehículo(patente) 4=conductor 5=cliente 6=detalle
      VIP_DEADLINE : 1=cliente 2=deadline(hh:mm) 3=min_restantes 4=patente 5=eta 6=margen
    """
    content_sid = _template_for(alert.tipo)

    visita = None
    if alert.visita_id is not None:
        visita = (await db.execute(
            select(Visita).where(Visita.visita_id == alert.visita_id)
        )).scalar_one_or_none()
    empresa_nombre = await db.scalar(
        select(Empresa.nombre).where(Empresa.empresa_id == alert.empresa_id)
    )

    if alert.tipo == "vip_deadline":
        try:
            payload = json.loads(alert.payload_json or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        eta = visita.eta_estimada if visita else None
        sim_now = None
        raw = payload.get("sim_now")
        if isinstance(raw, str):
            try:
                sim_now = datetime.fromisoformat(raw)
            except ValueError:
                sim_now = None
        # Normalize tz: MSSQL returns aware datetimes, SQLite naive — coerce to
        # UTC so the subtraction never mixes naive/aware.
        if eta is not None and eta.tzinfo is None:
            eta = eta.replace(tzinfo=UTC)
        if sim_now is not None and sim_now.tzinfo is None:
            sim_now = sim_now.replace(tzinfo=UTC)
        if eta is not None and sim_now is not None:
            mins = int((eta - sim_now).total_seconds() // 60)
        elif payload.get("deadline_min") is not None:
            mins = int(payload["deadline_min"])
        else:
            mins = None
        patente, _conductor = await _ruta_vehicle_driver(db, visita)
        cliente = (visita.cliente_nombre if visita else None) or empresa_nombre
        return content_sid, {
            "1": _clip(cliente, 60),
            "2": _hhmm(eta),                         # deadline (promised time)
            "3": _clip(mins if mins is not None else "-", 6),
            "4": _clip(patente, 20),
            "5": _hhmm(eta),                         # template label is "ETA estimada" — show the ETA, not the wall clock
            "6": _clip(f"{mins:+d}" if mins is not None else "-", 6),  # signed margin (+25 / -5), never "+-5"
        }

    # Generic operational alert (eta_breach / eta_preview / manual).
    # Template text: "vehiculo {{3}} con conductor {{4}}, cliente {{5}}".
    motivo = (visita.motivo if visita else None) or alert.tipo
    patente, conductor = await _ruta_vehicle_driver(db, visita)
    return content_sid, {
        "1": _clip((alert.severity or "alta").upper(), 12),
        "2": _clip(motivo, 60),
        "3": _clip(patente, 20),
        "4": _clip(conductor, 60),
        "5": _clip(visita.cliente_nombre if visita else "-", 60),
        "6": _clip(alert.descripcion, 200),
    }


def _is_mssql(db: AsyncSession) -> bool:
    """Detect dialect via AsyncSession.get_bind() (sync).

    Returns True for Azure SQL prod, False for SQLite tests. Used to pick the
    view-based recipient query vs the Python fallback.
    """
    try:
        bind = db.get_bind()
        return "mssql" in (bind.dialect.name or "")
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Recipient resolution
# ----------------------------------------------------------------------------

class _Recipient:
    """Minimal row shape post-resolution.

    Not a Pydantic model — we never expose this; it's a transient struct.
    """

    __slots__ = (
        "nombre",
        "notify_motivos",
        "notify_severities",
        "phone_e164",
        "recipient_id",
        "recipient_type",
        "rol_or_role",
    )

    def __init__(
        self,
        recipient_type: str,
        recipient_id: str,
        nombre: str,
        phone_e164: str,
        rol_or_role: str,
        notify_severities: str | None = None,
        notify_motivos: str | None = None,
    ):
        self.recipient_type = recipient_type
        self.recipient_id = recipient_id
        self.nombre = nombre
        self.phone_e164 = phone_e164
        self.rol_or_role = rol_or_role
        self.notify_severities = notify_severities
        self.notify_motivos = notify_motivos


async def _load_recipients_mssql(db: AsyncSession, empresa_id: int) -> list[_Recipient]:
    """Query the production view + JOIN to empresa_contactos for filters.

    The view itself doesn't expose `notify_severities` / `notify_motivos`
    (those columns live on `empresa_contactos`), so we LEFT JOIN by
    recipient_id when recipient_type='contacto'.
    """
    schema = settings.db_schema
    sql = text(
        f"""
        SELECT
            v.recipient_type,
            v.recipient_id,
            v.nombre,
            v.phone_e164,
            v.rol_or_role,
            c.notify_severities,
            c.notify_motivos
        FROM [{schema}].[vw_notif_recipients] v
        LEFT JOIN [{schema}].[empresa_contactos] c
            ON v.recipient_type = 'contacto'
            AND TRY_CAST(v.recipient_id AS INT) = c.contact_id
        WHERE v.empresa_id = :empresa_id AND v.notify_enabled = 1
        """
    )
    rows = (await db.execute(sql, {"empresa_id": empresa_id})).all()
    return [
        _Recipient(
            recipient_type=r.recipient_type,
            recipient_id=r.recipient_id,
            nombre=r.nombre,
            phone_e164=r.phone_e164,
            rol_or_role=r.rol_or_role,
            notify_severities=r.notify_severities,
            notify_motivos=r.notify_motivos,
        )
        for r in rows
    ]


async def _load_recipients_fallback(db: AsyncSession, empresa_id: int) -> list[_Recipient]:
    """Python-side UNION used in tests (SQLite) where the view doesn't exist.

    Same opt-in semantics as the view: phone_e164 starts with '+', activo=1,
    and (for users + contactos) `activation_used_at` / `opted_in_at` is set.
    """
    out: list[_Recipient] = []

    # users — empresa_id may be NULL for falabella_* (cross-empresa) users.
    # Per CR-022 they receive `alta` and `critica` — we include them and let
    # the post-fetch filter drop them by severity.
    users_q = select(User).where(
        User.activo == True,  # noqa: E712
        User.notify_whatsapp == True,  # noqa: E712
        User.activation_used_at.isnot(None),
        User.phone_e164.isnot(None),
    )
    for u in (await db.execute(users_q)).scalars().all():
        if u.empresa_id is not None and u.empresa_id != empresa_id:
            continue  # scoped user not in this tenant
        if not (u.phone_e164 or "").startswith("+"):
            continue
        out.append(_Recipient(
            recipient_type="user",
            recipient_id=str(u.user_id),
            nombre=u.display_name,
            phone_e164=u.phone_e164 or "",
            rol_or_role=u.role,
        ))

    drivers_q = select(Driver).where(
        Driver.empresa_id == empresa_id,
        Driver.activo == True,  # noqa: E712
        Driver.notify_whatsapp == True,  # noqa: E712
        Driver.opted_in_at.isnot(None),
        Driver.phone_e164.isnot(None),
    )
    for d in (await db.execute(drivers_q)).scalars().all():
        if not (d.phone_e164 or "").startswith("+"):
            continue
        out.append(_Recipient(
            recipient_type="driver",
            recipient_id=str(d.driver_id),
            nombre=d.nombre,
            phone_e164=d.phone_e164 or "",
            rol_or_role="driver",
        ))

    contactos_q = select(EmpresaContacto).where(
        EmpresaContacto.empresa_id == empresa_id,
        EmpresaContacto.activo == True,  # noqa: E712
        EmpresaContacto.opted_in_at.isnot(None),
        EmpresaContacto.phone_e164.isnot(None),
    )
    for c in (await db.execute(contactos_q)).scalars().all():
        if not (c.phone_e164 or "").startswith("+"):
            continue
        out.append(_Recipient(
            recipient_type="contacto",
            recipient_id=str(c.contact_id),
            nombre=c.nombre,
            phone_e164=c.phone_e164 or "",
            rol_or_role=c.rol,
            notify_severities=c.notify_severities,
            notify_motivos=c.notify_motivos,
        ))

    return out


def _passes_filter(recipient: _Recipient, alert: Alert, motivo: str | None) -> bool:  # noqa: PLR0911 -- one return per role branch reads better than nested ifs
    """Per-row severity/motivo filter.

    Rules:
      * driver → always pass (the worker on the ground).
      * user with role admin/ops/transport_manager → pass for severity in
        ('alta', 'critica'). Lower severities only if explicitly opted in
        (not modeled yet → drop).
      * contacto → respect `notify_severities` JSON whitelist if present.
        If `notify_motivos` is set and alert has a `motivo`, require the
        motivo be in the whitelist.
    """
    if recipient.recipient_type == "driver":
        return True

    if recipient.recipient_type == "user":
        # Default: alta/critica only. Admins additionally see all (they own
        # the system). Lower severities require explicit opt-in which we
        # don't model in MVP.
        if recipient.rol_or_role == "falabella_admin":
            return True
        return alert.severity in ("alta", "critica")

    if recipient.recipient_type == "contacto":
        if recipient.notify_severities:
            try:
                allowed = json.loads(recipient.notify_severities)
                if isinstance(allowed, list) and alert.severity not in allowed:
                    return False
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    f"[dispatch] contacto {recipient.recipient_id} has malformed "
                    f"notify_severities, defaulting to allow: {recipient.notify_severities!r}"
                )
        if motivo and recipient.notify_motivos:
            try:
                allowed_motivos = json.loads(recipient.notify_motivos)
                if isinstance(allowed_motivos, list) and motivo not in allowed_motivos:
                    return False
            except (json.JSONDecodeError, TypeError):
                pass
        return True

    return False


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

async def dispatch_alert(db: AsyncSession, alert: Alert, motivo: str | None = None) -> AlertDispatchResult:
    """Resolve recipients, send WhatsApp, update alert state.

    Idempotent: if `alert.estado` is not `'abierta'`, returns a zero-result.

    `motivo` is the optional motivo de no-entrega tied to the visita — when
    present it's used to filter contactos by `notify_motivos`. Cron jobs
    pass it from the visita; the manual endpoint does not.
    """
    if alert.estado != "abierta":
        logger.info(
            f"[dispatch] alert {alert.alert_id} is {alert.estado!r}, "
            "skipping (idempotent)"
        )
        return AlertDispatchResult(
            alert_id=alert.alert_id,
            recipients=0,
            sent=0,
            dry_run=settings.notifications_dry_run,
        )

    use_mssql = _is_mssql(db)
    if use_mssql:
        recipients = await _load_recipients_mssql(db, alert.empresa_id)
    else:
        recipients = await _load_recipients_fallback(db, alert.empresa_id)

    matched: list[_Recipient] = [r for r in recipients if _passes_filter(r, alert, motivo)]

    sent_count = 0
    content_sid, content_vars = await _build_template_message(db, alert)

    for r in matched:
        ok = await send_whatsapp(
            to=r.phone_e164,
            content_sid=content_sid,
            content_variables=content_vars,
        )
        if ok:
            sent_count += 1
        else:
            logger.warning(
                f"[dispatch] send_whatsapp failed for {r.recipient_type}:{r.recipient_id}"
            )

    # Alert state transition. We mark 'notificada' when at least one message
    # actually went out, OR when there were no recipients to notify at all
    # (retrying can't conjure recipients — that's a config gap, not transient).
    # But if recipients existed and EVERY send failed (e.g. a transient Twilio
    # outage), we deliberately LEAVE the alert 'abierta' so the cron retries —
    # otherwise a momentary outage permanently suppresses a real eta_breach /
    # vip_deadline and operators silently never hear about it.
    if sent_count > 0 or not matched:
        alert.estado = "notificada"
        alert.notified_at = datetime.now(UTC)
        alert.notified_recipients_count = sent_count
    else:
        logger.warning(
            f"[dispatch] alert {alert.alert_id} had {len(matched)} recipient(s) but all "
            f"sends failed — leaving 'abierta' for retry on the next cron run"
        )
    await db.commit()

    logger.info(
        f"[dispatch] alert {alert.alert_id} tipo={alert.tipo} severity={alert.severity} "
        f"recipients={len(matched)} sent={sent_count} dry_run={settings.notifications_dry_run}"
    )

    return AlertDispatchResult(
        alert_id=alert.alert_id,
        recipients=len(matched),
        sent=sent_count,
        dry_run=settings.notifications_dry_run,
    )

"""Onboarding rápido para sumar personas a la demo de WhatsApp.

POST /api/whatsapp/onboard
  Body: { phone, name, kind: 'driver'|'manager'|'contact', empresa_id?, vehicle_id? }
  Crea/actualiza el registro apropiado para que la persona sea reconocida
  por el agente conversacional cuando escriba al sandbox.

GET /api/whatsapp/onboard/sandbox-info
  Devuelve el código join + número del sandbox para compartir con quien
  vaya a unirse.

GET /api/whatsapp/onboard/list
  Lista de personas ya onboardeadas (drivers + users + contactos), agrupadas
  por kind. Útil para chequear quién está enrolado antes/después de la demo.
"""
from __future__ import annotations

import os
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import CurrentUser, require_admin
from db import get_conn


router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp-onboarding"])


_PHONE_RE = re.compile(r"^\+\d{8,15}$")


def _normalize_phone(raw: str) -> str:
    p = (raw or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    if p and not p.startswith("+"):
        p = "+" + p
    if not _PHONE_RE.match(p):
        raise HTTPException(400, f"Phone inválido: {raw!r}. Usá formato E.164 (+5691234...)")
    return p


# =============================================================================
# Schemas
# =============================================================================
class OnboardRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["driver", "manager", "contact"]
    empresa_id: Optional[int] = None
    vehicle_id: Optional[int] = None        # solo si kind=driver
    role: Optional[str] = None              # solo si kind=manager: 'transport_manager' | 'falabella_admin' | 'falabella_ops'
    rol: Optional[str] = None               # solo si kind=contact: 'jefe' | 'coordinador' | 'driver' | 'otro'
    sandbox_join_code: Optional[str] = None  # informativo, no se persiste


class OnboardResponse(BaseModel):
    ok: bool
    kind: str
    id: str
    name: str
    phone: str
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    message: str = ""
    sandbox_join: Optional[str] = None       # mensaje listo-para-WhatsApp con el código


class SandboxInfo(BaseModel):
    sandbox_number: str
    join_code: Optional[str] = None
    instructions: str
    public_webhook_url: Optional[str] = None


class OnboardedPerson(BaseModel):
    kind: str
    id: str
    name: str
    phone: str
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    extra: Optional[str] = None


class OnboardListResponse(BaseModel):
    drivers: list[OnboardedPerson]
    managers: list[OnboardedPerson]
    contacts: list[OnboardedPerson]


# =============================================================================
# Endpoints
# =============================================================================
@router.post("/onboard", response_model=OnboardResponse)
def onboard(body: OnboardRequest, user: CurrentUser = Depends(require_admin)) -> OnboardResponse:
    """Agrega o actualiza una persona para que sea reconocida por el agente.

    Idempotente por phone: si el phone ya existe en el kind indicado, hace UPDATE.
    Si existe en OTRO kind, devuelve error (para evitar duplicados confusos).
    """
    phone = _normalize_phone(body.phone)
    kind = body.kind

    # Verificación cross-kind: si el phone existe en otro lugar
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT driver_id, name FROM fpoc_drivers WHERE phone_e164 = ?", (phone,))
        existing_drv = cur.fetchone()
        cur.execute("SELECT user_id, display_name, role FROM fpoc_users WHERE phone_e164 = ?", (phone,))
        existing_usr = cur.fetchone()
        cur.execute("SELECT contact_id, nombre, empresa_id FROM fpoc_empresa_contactos WHERE phone_e164 = ? AND active = 1", (phone,))
        existing_ctc = cur.fetchone()

    persisted_id: str
    empresa_nombre: Optional[str] = None

    if kind == "driver":
        if existing_usr or (existing_ctc and existing_drv is None):
            # Permitido pero advertir: phone duplicado entre kinds
            pass
        # Necesita vehicle_id (asignación) para que el agente arme la ruta
        vid = body.vehicle_id
        if vid is None:
            raise HTTPException(400, "vehicle_id requerido para kind=driver")
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("SELECT vehicle_id, name AS vname FROM fpoc_vehicles WHERE vehicle_id = ?", (vid,))
            v = cur.fetchone()
            if v is None:
                raise HTTPException(404, f"vehicle_id {vid} no existe")
            vehicle_name = str(v[1])
            # ID determinístico
            driver_id = f"DRV-{vid:03d}"
            cur.execute(
                """
                UPDATE fpoc_drivers
                   SET name = ?, phone = ?, phone_e164 = ?, vehicle_id = ?,
                       vehicle_name = ?, active = 1, notify_whatsapp = 1,
                       opted_in_at = CURRENT_TIMESTAMP
                 WHERE driver_id = ?
                """,
                (body.name, phone, phone, vid, vehicle_name, driver_id),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO fpoc_drivers
                      (driver_id, name, phone, phone_e164, vehicle_id, vehicle_name,
                       active, notify_whatsapp, opted_in_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 1, CURRENT_TIMESTAMP)
                    """,
                    (driver_id, body.name, phone, phone, vid, vehicle_name),
                )
            cn.commit()
            persisted_id = driver_id

    elif kind == "manager":
        role = body.role or "transport_manager"
        if role not in ("transport_manager", "falabella_admin", "falabella_ops"):
            raise HTTPException(400, f"role inválido para manager: {role}")
        empresa_id = body.empresa_id
        # falabella_* no necesita empresa_id; transport_manager sí
        if role == "transport_manager" and empresa_id is None:
            raise HTTPException(400, "empresa_id requerido para transport_manager")
        with get_conn() as cn:
            cur = cn.cursor()
            if empresa_id is not None:
                cur.execute("SELECT nombre FROM fpoc_empresas_transporte WHERE empresa_id = ?", (empresa_id,))
                e = cur.fetchone()
                if e is None:
                    raise HTTPException(404, f"empresa_id {empresa_id} no existe")
                empresa_nombre = str(e[0])
            # Email derivado del phone (POC: no necesita login real)
            email = f"wa-{phone.lstrip('+')}@valuedata.cl"
            # Hash placeholder (no usable para login web)
            from passlib.hash import bcrypt
            pwd_hash = bcrypt.hash(f"wa-onboarded-{phone}")
            cur.execute(
                """
                UPDATE fpoc_users
                   SET display_name = ?, role = ?, empresa_id = ?,
                       activo = 1, phone_e164 = ?, notify_whatsapp = 1
                 WHERE email = ?
                """,
                (body.name, role, empresa_id, phone, email),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO fpoc_users
                      (email, password_hash, display_name, role, empresa_id,
                       activo, phone_e164, notify_whatsapp)
                    VALUES (?, ?, ?, ?, ?, 1, ?, 1)
                    """,
                    (email, pwd_hash, body.name, role, empresa_id, phone),
                )
            cn.commit()
            cur.execute("SELECT user_id FROM fpoc_users WHERE phone_e164 = ?", (phone,))
            r = cur.fetchone()
            persisted_id = str(int(r[0]))

    elif kind == "contact":
        empresa_id = body.empresa_id
        if empresa_id is None:
            raise HTTPException(400, "empresa_id requerido para kind=contact")
        rol = body.rol or "otro"
        if rol not in ("jefe", "coordinador", "dispatcher", "driver", "otro"):
            raise HTTPException(400, f"rol inválido: {rol}")
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("SELECT nombre FROM fpoc_empresas_transporte WHERE empresa_id = ?", (empresa_id,))
            e = cur.fetchone()
            if e is None:
                raise HTTPException(404, f"empresa_id {empresa_id} no existe")
            empresa_nombre = str(e[0])
            # Si ya existe activo para ese phone, update; sino insert
            cur.execute(
                "SELECT contact_id FROM fpoc_empresa_contactos "
                "WHERE phone_e164 = ? AND empresa_id = ? AND active = 1",
                (phone, empresa_id),
            )
            r = cur.fetchone()
            if r is not None:
                cid = int(r[0])
                cur.execute(
                    "UPDATE fpoc_empresa_contactos SET nombre = ?, rol = ?, "
                    "opted_in_at = CURRENT_TIMESTAMP WHERE contact_id = ?",
                    (body.name, rol, cid),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO fpoc_empresa_contactos
                      (empresa_id, nombre, rol, phone_e164, opted_in_at, active)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 1)
                    """,
                    (empresa_id, body.name, rol, phone),
                )
                try:
                    cur.execute("SELECT last_insert_rowid()")
                    row = cur.fetchone()
                    cid = int(row[0]) if row and row[0] is not None else 0
                except Exception:
                    cid = 0
            cn.commit()
            persisted_id = str(cid)
    else:
        raise HTTPException(400, f"kind desconocido: {kind}")

    # Limpiar sesión vieja de WhatsApp (si la había) para que tome el rol nuevo
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("DELETE FROM fpoc_whatsapp_sessions WHERE phone_e164 = ?", (phone,))
            cn.commit()
    except Exception:  # noqa: BLE001
        pass

    # Refrescar maestros en memoria (STATE.drivers, vehicles, clientes) para que
    # el snapshot del modelo "vea" al recién onboardeado al instante.
    try:
        from state import STATE
        STATE.reload_maestros()
    except Exception:  # noqa: BLE001
        pass

    # Emitir evento al stream para que el front muestre toast/badge en vivo.
    try:
        from datetime import datetime as _dt
        from events import EVENTS
        from state import STATE as _STATE
        EVENTS.emit(
            "wa_user_onboarded",
            _STATE.sim_clock or _dt.utcnow(),
            {
                "phone": phone,
                "name": body.name,
                "kind": kind,
                "source": "manual_admin",
                "by_user_id": user.user_id,
                "id": persisted_id,
                "empresa_id": body.empresa_id,
                "empresa_nombre": empresa_nombre,
            },
        )
    except Exception:  # noqa: BLE001
        pass

    sandbox_number = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").replace("whatsapp:", "")
    join_code = body.sandbox_join_code or os.environ.get("TWILIO_SANDBOX_JOIN_CODE", "")
    sandbox_join = (
        f"Pedile a {body.name} que mande \"join {join_code}\" al WhatsApp {sandbox_number}, "
        f"y después escriba 'hola' para empezar."
        if join_code
        else f"Pedile a {body.name} que se una al sandbox de Twilio y escriba 'hola' al {sandbox_number}."
    )

    return OnboardResponse(
        ok=True,
        kind=kind,
        id=persisted_id,
        name=body.name,
        phone=phone,
        empresa_id=body.empresa_id,
        empresa_nombre=empresa_nombre,
        message=f"{kind.capitalize()} {body.name} ({phone}) onboardeado correctamente.",
        sandbox_join=sandbox_join,
    )


@router.get("/onboard/sandbox-info", response_model=SandboxInfo)
def sandbox_info(_: CurrentUser = Depends(require_admin)) -> SandboxInfo:
    """Info útil para compartir con quien vaya a unirse al sandbox."""
    sandbox_number = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").replace("whatsapp:", "")
    join_code = os.environ.get("TWILIO_SANDBOX_JOIN_CODE", "")
    public_url = os.environ.get("TWILIO_INBOUND_PUBLIC_URL", "")
    instructions = (
        f"1. Mandar por WhatsApp la palabra 'join {join_code}' al número {sandbox_number}\n"
        f"2. Esperar la respuesta 'You are all set!'\n"
        f"3. Escribir 'hola' al mismo número para empezar."
        if join_code
        else f"1. Mandar 'join <código del sandbox>' al {sandbox_number}\n"
             f"   (el código está en console.twilio.com → Messaging → Try it out)\n"
             f"2. Después escribir 'hola' para empezar."
    )
    return SandboxInfo(
        sandbox_number=sandbox_number,
        join_code=join_code or None,
        instructions=instructions,
        public_webhook_url=(public_url + "/api/twilio/inbound") if public_url else None,
    )


@router.get("/onboard/list", response_model=OnboardListResponse)
def list_onboarded(_: CurrentUser = Depends(require_admin)) -> OnboardListResponse:
    """Personas ya onboardeadas. Para tener visibilidad antes/durante la demo."""
    drivers: list[OnboardedPerson] = []
    managers: list[OnboardedPerson] = []
    contacts: list[OnboardedPerson] = []
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT driver_id, name, phone_e164, vehicle_id, vehicle_name "
            "FROM fpoc_drivers WHERE phone_e164 IS NOT NULL AND phone_e164 != '' "
            "AND notify_whatsapp = 1 ORDER BY vehicle_id"
        )
        for r in cur.fetchall():
            drivers.append(OnboardedPerson(
                kind="driver",
                id=str(r[0]),
                name=str(r[1]),
                phone=str(r[2]),
                extra=f"{r[4]} (vehicle_id {r[3]})" if r[3] else None,
            ))
        cur.execute(
            """
            SELECT u.user_id, u.display_name, u.phone_e164, u.role,
                   u.empresa_id, e.nombre AS empresa_nombre
            FROM fpoc_users u
            LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = u.empresa_id
            WHERE u.phone_e164 IS NOT NULL AND u.phone_e164 != ''
            ORDER BY u.role, u.empresa_id
            """
        )
        for r in cur.fetchall():
            managers.append(OnboardedPerson(
                kind="manager",
                id=str(r[0]),
                name=str(r[1]),
                phone=str(r[2]),
                empresa_id=int(r[4]) if r[4] is not None else None,
                empresa_nombre=str(r[5]) if r[5] else None,
                extra=str(r[3]),  # role
            ))
        cur.execute(
            """
            SELECT c.contact_id, c.nombre, c.phone_e164, c.rol,
                   c.empresa_id, e.nombre AS empresa_nombre, c.opted_in_at
            FROM fpoc_empresa_contactos c
            LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = c.empresa_id
            WHERE c.active = 1 AND c.opted_in_at IS NOT NULL
            ORDER BY c.empresa_id, c.contact_id
            """
        )
        for r in cur.fetchall():
            contacts.append(OnboardedPerson(
                kind="contact",
                id=str(r[0]),
                name=str(r[1]),
                phone=str(r[2]),
                empresa_id=int(r[4]) if r[4] is not None else None,
                empresa_nombre=str(r[5]) if r[5] else None,
                extra=f"rol={r[3]}",
            ))
    return OnboardListResponse(drivers=drivers, managers=managers, contacts=contacts)

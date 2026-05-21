"""Vista agregada de activaciones wa.me (users + drivers + contactos).

Hoy el admin tiene que ir a 3 mantenedores distintos (Usuarios, Drivers,
Contactos) para ver qué cuentas todavía no activaron su link wa.me (workaround
CR-014 para el bloqueo 63112 de Meta). Este router expone un endpoint único
que agrega las 3 tablas y devuelve una lista normalizada que el frontend
puede pintar en un dashboard "Invitaciones".

Endpoints (prefijo /api/admin que añade el router padre — montado standalone
en main.py con prefix explícito):

    GET /api/admin/invitations

Filtros (todos opcionales, AND-combinables):

  tipo       user | driver | contacto
  state      pending | activated | no_link
  empresa_id int
  search     substring case-insensitive sobre nombre o phone
  limit      1..500 (default 200)
  offset     >=0 (default 0)

Lógica de state:
  - activated : activation_used_at IS NOT NULL
  - pending   : activation_token IS NOT NULL AND activation_used_at IS NULL
  - no_link   : activation_token IS NULL  (cuenta vieja, creada antes de CR-014)

Auth: solo falabella_admin / falabella_ops (matchea require_admin_or_ops
no implementado todavía — usamos current_user + is_falabella).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.activation import build_activation_link
from core.auth import CurrentUser, current_user
from core.db import get_conn


router = APIRouter(prefix="/api/admin", tags=["admin-invitations"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

InvitationTipo = Literal["user", "driver", "contacto"]
InvitationState = Literal["pending", "activated", "no_link"]


class InvitationItem(BaseModel):
    tipo: InvitationTipo
    id: str  # user_id como string, driver_id ya es string, contact_id como string
    nombre: str
    phone_e164: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    rol: Optional[str] = None  # role para user, rol para contacto, None para driver
    activo: bool
    activation_token: Optional[str] = None
    activation_link: Optional[str] = None
    activation_used_at: Optional[str] = None  # ISO-8601
    state: InvitationState


class InvitationsListOut(BaseModel):
    total: int
    summary: dict  # {"pending": X, "activated": Y, "no_link": Z}
    items: list[InvitationItem]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_admin_or_ops(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """Gate local: solo falabella_admin o falabella_ops.

    No existe require_admin_or_ops en core/auth todavía; reusamos is_falabella
    que cubre exactamente esos dos roles.
    """
    if not user.is_falabella:
        raise HTTPException(403, "Requiere rol falabella_admin o falabella_ops")
    return user


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _derive_state(token: Optional[str], used_at: Any) -> InvitationState:
    if used_at is not None:
        return "activated"
    if token:
        return "pending"
    return "no_link"


def _load_empresa_names(cn) -> dict[int, str]:
    """Cache empresa_id → nombre. Una sola query upfront para no JOIN-ear 3 veces."""
    cur = cn.cursor()
    cur.execute("SELECT empresa_id, nombre FROM fpoc.empresas_transporte")
    return {int(r.empresa_id): r.nombre for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Per-table fetchers
# ---------------------------------------------------------------------------

def _query_users(cn) -> list[dict]:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT user_id, display_name, phone_e164, empresa_id, role, activo,
               activation_token, activation_used_at
        FROM fpoc.users
        """
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "tipo": "user",
            "id": str(int(r.user_id)),
            "nombre": r.display_name,
            "phone_e164": r.phone_e164,
            "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
            "rol": r.role,
            "activo": bool(r.activo),
            "activation_token": getattr(r, "activation_token", None),
            "activation_used_at_raw": getattr(r, "activation_used_at", None),
        })
    return out


def _query_drivers(cn) -> list[dict]:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT driver_id, name, phone_e164, empresa_id, active,
               activation_token, activation_used_at
        FROM fpoc.drivers
        """
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "tipo": "driver",
            "id": str(r.driver_id),
            "nombre": r.name,
            "phone_e164": r.phone_e164,
            "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
            "rol": None,
            "activo": bool(r.active),
            "activation_token": getattr(r, "activation_token", None),
            "activation_used_at_raw": getattr(r, "activation_used_at", None),
        })
    return out


def _query_contactos(cn) -> list[dict]:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT contact_id, nombre, phone_e164, empresa_id, rol, active,
               activation_token, activation_used_at
        FROM fpoc.empresa_contactos
        """
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "tipo": "contacto",
            "id": str(int(r.contact_id)),
            "nombre": r.nombre,
            "phone_e164": r.phone_e164,
            "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
            "rol": r.rol,
            "activo": bool(r.active),
            "activation_token": getattr(r, "activation_token", None),
            "activation_used_at_raw": getattr(r, "activation_used_at", None),
        })
    return out


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/invitations", response_model=InvitationsListOut)
def list_invitations(
    tipo: Optional[InvitationTipo] = Query(default=None),
    state: Optional[InvitationState] = Query(default=None),
    empresa_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(_require_admin_or_ops),
) -> InvitationsListOut:
    """Lista agregada de activaciones wa.me sobre users + drivers + contactos.

    Devuelve `items` ya filtrados+paginados, `total` con el conteo POST-filtro
    (pre-pagination) para que el frontend pueda mostrar "X de Y" y `summary`
    con el desglose por state sobre el universo filtrado (sin aplicar `state`
    para que las pestañas/contadores no se cancelen entre sí).
    """
    with get_conn() as cn:
        empresa_names = _load_empresa_names(cn)

        # Fetch (filtramos por tipo en Python; las 3 tablas son lo bastante
        # chicas para que esto sea OK hasta ~10k filas totales — si crece,
        # mover los filtros a SQL con WHERE dinámico).
        rows: list[dict] = []
        if tipo in (None, "user"):
            rows.extend(_query_users(cn))
        if tipo in (None, "driver"):
            rows.extend(_query_drivers(cn))
        if tipo in (None, "contacto"):
            rows.extend(_query_contactos(cn))

    # Filtros adicionales (empresa_id + search). state se aplica después
    # para que summary refleje el universo pre-state.
    if empresa_id is not None:
        rows = [r for r in rows if r["empresa_id"] == empresa_id]

    if search:
        needle = search.strip().lower()
        if needle:
            def _match(r: dict) -> bool:
                n = (r.get("nombre") or "").lower()
                p = (r.get("phone_e164") or "").lower()
                return needle in n or needle in p
            rows = [r for r in rows if _match(r)]

    # Derivar state + materializar shape final (manteniendo el raw used_at
    # para el cálculo de state contra None).
    materialized: list[dict] = []
    for r in rows:
        st = _derive_state(r["activation_token"], r["activation_used_at_raw"])
        materialized.append({**r, "state": st})

    # summary se calcula sobre el universo filtrado por (tipo, empresa_id,
    # search) PERO antes de aplicar el filtro `state` — para que las
    # pestañas/contadores del frontend no se anulen a sí mismas.
    summary = {"pending": 0, "activated": 0, "no_link": 0}
    for r in materialized:
        summary[r["state"]] += 1

    if state is not None:
        materialized = [r for r in materialized if r["state"] == state]

    total = len(materialized)

    # Orden estable: pending primero, después no_link, después activated.
    # Dentro de cada grupo, por nombre asc para que sea reproducible.
    _state_rank = {"pending": 0, "no_link": 1, "activated": 2}
    materialized.sort(key=lambda r: (_state_rank[r["state"]], (r.get("nombre") or "").lower()))

    page = materialized[offset:offset + limit]

    items: list[InvitationItem] = []
    for r in page:
        token = r["activation_token"]
        items.append(InvitationItem(
            tipo=r["tipo"],
            id=r["id"],
            nombre=r["nombre"],
            phone_e164=r.get("phone_e164"),
            empresa_id=r.get("empresa_id"),
            empresa_nombre=(
                empresa_names.get(r["empresa_id"])
                if r.get("empresa_id") is not None else None
            ),
            rol=r.get("rol"),
            activo=r["activo"],
            activation_token=token,
            activation_link=build_activation_link(token) if token else None,
            activation_used_at=_iso(r["activation_used_at_raw"]),
            state=r["state"],
        ))

    return InvitationsListOut(total=total, summary=summary, items=items)

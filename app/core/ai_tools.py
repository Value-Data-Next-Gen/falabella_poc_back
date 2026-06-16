"""AI assistant tools — functions the LLM can call to query operational data."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alert import Alert
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.document import DocumentType, EntityDocument
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.motivo import Motivo
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita

# Actor identifies the principal that originated the chat turn:
#   - User: web /api/v1/chat (browser session, JWT in cookie)
#   - Driver: WhatsApp inbound from a driver phone
#   - EmpresaContacto: WhatsApp inbound from a jefe/coordinador phone
#   - None: chat invoked without authentication (should not happen in prod;
#     defensive default used in tests / future surfaces)
Actor = User | Driver | EmpresaContacto | None


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "contar_entidades",
            "description": "Cuenta conductores, vehiculos, contactos o empresas. Puede filtrar por empresa_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entidad": {"type": "string", "enum": ["conductores", "vehiculos", "contactos", "empresas"]},
                    "empresa_id": {"type": "integer", "description": "Filtrar por empresa (opcional)"},
                    "solo_activos": {"type": "boolean", "default": True},
                },
                "required": ["entidad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_conductores",
            "description": "Lista conductores con su estado de activacion WhatsApp y vehiculo asignado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "empresa_id": {"type": "integer"},
                    "solo_sin_activar": {"type": "boolean", "default": False},
                    "limite": {"type": "integer", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verificar_compliance_documentos",
            "description": "Verifica que documentos obligatorios faltan o estan vencidos para un conductor, vehiculo o empresa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo_entidad": {"type": "string", "enum": ["conductor", "vehiculo", "empresa"]},
                    "entidad_id": {"type": "string", "description": "ID de la entidad (ej: DRV-01001, 1, etc)"},
                },
                "required": ["tipo_entidad", "entidad_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_motivos",
            "description": "Lista todos los motivos de no-entrega activos del catalogo oficial.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clasificar_motivo",
            "description": "Analiza el comentario de un conductor sobre una entrega fallida y sugiere el motivo correcto del catalogo oficial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo_reportado": {"type": "string", "description": "Motivo que reporto el conductor"},
                    "comentario": {"type": "string", "description": "Texto libre del conductor explicando que paso"},
                },
                "required": ["motivo_reportado", "comentario"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resumen_empresa",
            "description": "Genera un resumen operativo de una empresa: cuantos conductores, vehiculos, contactos, documentos pendientes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "empresa_id": {"type": "integer"},
                },
                "required": ["empresa_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_alertas_abiertas",
            "description": (
                "Lista las alertas operativas en estado abierta o notificada. "
                "Útil para responder preguntas tipo '¿qué problemas hay hoy?' o "
                "'¿qué visitas están atrasadas?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "empresa_id": {"type": "integer", "description": "Filtrar por empresa (opcional)"},
                    "dia_id": {"type": "integer", "description": "Filtrar por día operativo (opcional)"},
                    "severity": {
                        "type": "string",
                        "enum": ["baja", "media", "alta", "critica"],
                        "description": "Filtrar por severidad (opcional)",
                    },
                    "tipo": {
                        "type": "string",
                        "enum": ["eta_breach", "eta_preview", "vip_deadline", "manual"],
                        "description": "Filtrar por tipo (opcional)",
                    },
                    "limit": {"type": "integer", "default": 20, "maximum": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crear_alerta_manual",
            "description": (
                "Crea una alerta operativa manual. Solo cuando el operador o conductor "
                "reporta un problema explícito que requiera acción inmediata "
                "(ej. 'siniestro en calle', 'cliente reportó queja', 'conductor avisa "
                "demora prolongada'). NO usar para resumir información, solo para "
                "registrar incidentes accionables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "empresa_id": {
                        "type": "integer",
                        "description": "Empresa a la que pertenece (requerido)",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["baja", "media", "alta", "critica"],
                        "description": "Severidad estimada por el LLM en base al contexto",
                    },
                    "descripcion": {
                        "type": "string",
                        "description": "Descripción accionable de la alerta, máximo 500 chars",
                    },
                    "dia_id": {
                        "type": "integer",
                        "description": "Día operativo relacionado (opcional)",
                    },
                    "visita_id": {
                        "type": "integer",
                        "description": "Visita específica afectada (opcional)",
                    },
                    "auto_dispatch": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Si true, dispara WhatsApp a los recipients (solo cuando es "
                            "severity alta/critica)"
                        ),
                    },
                },
                "required": ["empresa_id", "severity", "descripcion"],
            },
        },
    },
    # ── CR-024 — cliente master tools ───────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "obtener_info_cliente_por_folio",
            "description": (
                "Obtiene información operativa de un cliente buscado por su "
                "folio de cliente (do de Falabella). Útil para responder "
                "preguntas del conductor sobre un destinatario específico. "
                "SIEMPRE llamá este tool si el conductor menciona un folio o "
                "número de pedido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folio_cliente": {
                        "type": "string",
                        "description": (
                            "Folio cliente (do) — número que el conductor "
                            "menciona"
                        ),
                    },
                },
                "required": ["folio_cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancelar_visita_manual",
            "description": (
                "Cancela una visita específica con motivo. Solo cuando se "
                "reporta una cancelación legítima (cliente no disponible, "
                "dirección incorrecta confirmada, pedido rechazado, etc.). "
                "NO usar para no_entregado por motivos transitorios (sin "
                "morador, calle cerrada) — esos se manejan en "
                "/motivos/clasificar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "visita_id": {"type": "integer"},
                    "motivo": {
                        "type": "string",
                        "description": (
                            "Motivo de la cancelación, conciso. Máximo 200 "
                            "chars."
                        ),
                    },
                },
                "required": ["visita_id", "motivo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obtener_reporte",
            "description": (
                "Genera un reporte operativo (visitas, % de exito, puntualidad, "
                "motivos de no-entrega, peor conductor) de una empresa. Por "
                "defecto cubre el ultimo dia operativo; usa `rango_dias` para "
                "agregar varios dias (ej. 7 para la semana). Util para 'reporte "
                "de hoy', 'como vamos', 'reporte de la semana'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "empresa_id": {
                        "type": "integer",
                        "description": "Empresa a reportar (opcional para roles con una sola empresa).",
                    },
                    "rango_dias": {
                        "type": "integer",
                        "default": 1,
                        "description": "Cuantos dias operativos recientes incluir (1 = ultimo dia).",
                    },
                },
            },
        },
    },
]


# ----------------------------------------------------------------------------
# Role-based tool exposure.
#
# Tenant scope (below) stops cross-empresa reads, but a *driver* still
# shouldn't be offered admin tooling like "lista todos los conductores" or
# "resumen de la empresa". We additionally gate which tools each actor type is
# even *shown*, so the LLM answers each user with role-appropriate capabilities.
#
# Roles: driver | contacto (jefe/coordinador) | manager (transport_manager) |
# falabella (falabella_admin/ops, full ops). `anon` (no actor) gets nothing.
# ----------------------------------------------------------------------------

_DRIVER_TOOLS = {
    "clasificar_motivo",
    "listar_motivos",
    "obtener_info_cliente_por_folio",
    "cancelar_visita_manual",
    "crear_alerta_manual",
}
# Oversight roles (contacto / manager / falabella) get the driver tools plus
# the read/aggregate ops tools and the report tool.
_OVERSIGHT_TOOLS = _DRIVER_TOOLS | {
    "listar_alertas_abiertas",
    "contar_entidades",
    "listar_conductores",
    "resumen_empresa",
    "verificar_compliance_documentos",
    "obtener_reporte",
}
_TOOLS_BY_ROLE: dict[str, set[str]] = {
    "driver": _DRIVER_TOOLS,
    "contacto": _OVERSIGHT_TOOLS,
    "manager": _OVERSIGHT_TOOLS,
    "falabella": _OVERSIGHT_TOOLS,
    "anon": set(),
}


def actor_role(actor: Actor) -> str:
    """Coarse role label used for tool exposure + prompt tailoring."""
    if isinstance(actor, Driver):
        return "driver"
    if isinstance(actor, EmpresaContacto):
        return "contacto"
    if isinstance(actor, User):
        return "falabella" if actor.role in ("falabella_admin", "falabella_ops") else "manager"
    return "anon"


def tool_definitions_for(actor: Actor) -> list[dict]:
    """The subset of TOOL_DEFINITIONS this actor is allowed to invoke."""
    allowed = _TOOLS_BY_ROLE.get(actor_role(actor), set())
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed]


# ----------------------------------------------------------------------------
# Actor scope helpers (used by alerts tools).
#
# Web chat ships a `User` (resolved by JWT cookie). WhatsApp inbound has no
# logged-in user — `_ai_reply` resolves the sender to a Driver / Contacto /
# User by phone. Tools that touch tenant data MUST gate on whichever one is
# present so the LLM can't be social-engineered into reading another tenant.
# ----------------------------------------------------------------------------


def _actor_can_access(actor: Actor, empresa_id: int) -> bool:
    """Return True iff `actor` is allowed to read/write empresa `empresa_id`."""
    if actor is None:
        return False
    if isinstance(actor, User):
        # falabella_admin / falabella_ops see everything; transport_manager
        # only their assigned empresas (loaded by `current_user` into the
        # private `_empresa_ids` attribute).
        if actor.role in ("falabella_admin", "falabella_ops"):
            return True
        return empresa_id in getattr(actor, "_empresa_ids", [])
    if isinstance(actor, Driver | EmpresaContacto):
        return actor.empresa_id == empresa_id
    return False


def _actor_default_empresa_id(actor: Actor) -> int | None:
    """Return the empresa_id the LLM should default to when not supplied.

    Drivers and contactos are bound to exactly one empresa; we use that. For
    Users we don't guess (could be cross-tenant).
    """
    if isinstance(actor, Driver | EmpresaContacto):
        return actor.empresa_id
    return None


def _actor_is_falabella_admin_or_ops(actor: Actor) -> bool:
    return isinstance(actor, User) and actor.role in ("falabella_admin", "falabella_ops")


def _resolve_scope(actor: Actor, requested: int | None) -> tuple[int | None, str | None]:
    """Resolve the empresa_id an info tool may use, enforcing tenant scope.

    Returns (empresa_id, error). A returned empresa_id of None means "all
    empresas" and is ONLY ever returned for falabella_admin/ops — every other
    actor is floored to an empresa they own, so the LLM can't be talked into
    reading another tenant's data.
    """
    if actor is None:
        return None, "Sin contexto de actor — no se puede consultar."
    if _actor_is_falabella_admin_or_ops(actor):
        return requested, None  # may be None (all) or a specific empresa
    if isinstance(actor, Driver | EmpresaContacto):
        return actor.empresa_id, None  # always forced to own empresa
    # transport_manager (User, non-admin): only their assigned empresas.
    ids = getattr(actor, "_empresa_ids", []) or []
    if requested is not None:
        return (requested, None) if requested in ids else (None, "Fuera de tu alcance.")
    if len(ids) == 1:
        return ids[0], None
    return None, "Especifica una empresa dentro de tu alcance."


async def execute_tool(  # noqa: PLR0911 -- one branch per tool reads better than a lookup table
    db: AsyncSession,
    name: str,
    args: dict[str, Any],
    actor: Actor = None,
) -> str:
    """Dispatch a tool call from the LLM.

    `actor` is whoever originated the chat turn (web user, WhatsApp driver,
    WhatsApp contacto). Required by tools that touch tenant-scoped data
    (alerts). Read-only legacy tools ignore it for now to keep behavior
    identical to pre-CR-022 chat turns.
    """
    # Defense-in-depth: the LLM is only *shown* role-appropriate tools, but
    # never trust that — reject any call outside the actor's allow-list.
    if name not in _TOOLS_BY_ROLE.get(actor_role(actor), set()):
        return json.dumps({"error": "Herramienta no disponible para tu rol."})
    if name == "contar_entidades":
        return await _contar_entidades(db, actor=actor, **args)
    elif name == "listar_conductores":
        return await _listar_conductores(db, actor=actor, **args)
    elif name == "verificar_compliance_documentos":
        return await _verificar_compliance(db, actor=actor, **args)
    elif name == "listar_motivos":
        return await _listar_motivos(db)
    elif name == "clasificar_motivo":
        return await _clasificar_motivo(db, **args)
    elif name == "resumen_empresa":
        return await _resumen_empresa(db, actor=actor, **args)
    elif name == "listar_alertas_abiertas":
        return await _listar_alertas_abiertas(db, actor=actor, **args)
    elif name == "crear_alerta_manual":
        return await _crear_alerta_manual(db, actor=actor, **args)
    elif name == "obtener_info_cliente_por_folio":
        return await _obtener_info_cliente_por_folio(db, actor=actor, **args)
    elif name == "cancelar_visita_manual":
        return await _cancelar_visita_manual(db, actor=actor, **args)
    elif name == "obtener_reporte":
        return await _obtener_reporte(db, actor=actor, **args)
    return json.dumps({"error": f"Tool {name} not found"})


async def _contar_entidades(db: AsyncSession, entidad: str, empresa_id: int | None = None, solo_activos: bool = True, actor: Actor = None) -> str:
    model_map = {"conductores": Driver, "vehiculos": Vehicle, "contactos": EmpresaContacto, "empresas": Empresa}
    model = model_map.get(entidad)
    if not model:
        return json.dumps({"error": f"Entidad desconocida: {entidad}"})
    eid, err = _resolve_scope(actor, empresa_id)
    if err:
        return json.dumps({"error": err})
    stmt = select(func.count()).select_from(model)
    if solo_activos and hasattr(model, 'activo'):
        stmt = stmt.where(model.activo == True)  # noqa: E712
    if eid and hasattr(model, 'empresa_id'):
        stmt = stmt.where(model.empresa_id == eid)
    result = await db.execute(stmt)
    count = result.scalar_one()
    return json.dumps({"entidad": entidad, "empresa_id": eid, "total": count})


async def _listar_conductores(db: AsyncSession, empresa_id: int | None = None, solo_sin_activar: bool = False, limite: int = 10, actor: Actor = None) -> str:
    eid, err = _resolve_scope(actor, empresa_id)
    if err:
        return json.dumps({"error": err})
    stmt = select(Driver).where(Driver.activo == True)  # noqa: E712
    if eid:
        stmt = stmt.where(Driver.empresa_id == eid)
    if solo_sin_activar:
        stmt = stmt.where(Driver.opted_in_at.is_(None))
    stmt = stmt.limit(limite)
    result = await db.execute(stmt)
    drivers = [
        {"driver_id": d.driver_id, "nombre": d.nombre, "empresa_id": d.empresa_id,
         "phone": d.phone_e164, "whatsapp_activo": d.opted_in_at is not None,
         "vehicle_id": d.vehicle_id}
        for d in result.scalars().all()
    ]
    return json.dumps({"conductores": drivers, "total": len(drivers)})


async def _verificar_compliance(db: AsyncSession, tipo_entidad: str, entidad_id: str, actor: Actor = None) -> str:
    from datetime import UTC, datetime

    # Tenant scope: resolve the entity's owning empresa and gate on the actor.
    owner: int | None = None
    if tipo_entidad == "empresa":
        owner = int(entidad_id) if str(entidad_id).isdigit() else None
    elif tipo_entidad == "conductor":
        owner = await db.scalar(select(Driver.empresa_id).where(Driver.driver_id == entidad_id))
    elif tipo_entidad == "vehiculo":
        owner = (await db.scalar(select(Vehicle.empresa_id).where(Vehicle.vehicle_id == int(entidad_id)))
                 if str(entidad_id).isdigit() else None)
    if owner is None:
        return json.dumps({"error": "Entidad no encontrada"})
    if not _actor_can_access(actor, owner):
        return json.dumps({"error": "Fuera de tu alcance."})

    today = datetime.now(UTC).date()

    types_result = await db.execute(
        select(DocumentType).where(DocumentType.entity_type == tipo_entidad, DocumentType.active == True)  # noqa: E712
    )
    doc_types = types_result.scalars().all()

    docs_result = await db.execute(
        select(EntityDocument).where(EntityDocument.entity_type == tipo_entidad, EntityDocument.entity_id == entidad_id)
    )
    all_docs = docs_result.scalars().all()

    items = []
    for dt in doc_types:
        matching = [d for d in all_docs if d.tipo == dt.codigo]
        latest = max(matching, key=lambda d: d.uploaded_at) if matching else None
        if not matching:
            status = "falta" if dt.mandatory else "opcional"
        elif latest and latest.expires_at and latest.expires_at < today:
            status = "vencido"
        else:
            status = "ok"
        items.append({"documento": dt.nombre, "codigo": dt.codigo, "obligatorio": dt.mandatory, "estado": status})

    faltantes = [i for i in items if i["estado"] == "falta"]
    vencidos = [i for i in items if i["estado"] == "vencido"]
    return json.dumps({"tipo_entidad": tipo_entidad, "entidad_id": entidad_id,
                        "documentos": items, "faltantes": len(faltantes), "vencidos": len(vencidos)})


async def _listar_motivos(db: AsyncSession) -> str:
    result = await db.execute(select(Motivo).where(Motivo.activo == True).order_by(Motivo.orden, Motivo.motivo_id))  # noqa: E712
    motivos = [
        {"codigo": m.codigo, "descripcion": m.descripcion, "severity": m.severity,
         "alertable": m.alertable, "desambiguacion": m.desambiguacion}
        for m in result.scalars().all()
    ]
    return json.dumps({"motivos": motivos, "total": len(motivos)})


async def _clasificar_motivo(db: AsyncSession, motivo_reportado: str, comentario: str) -> str:
    result = await db.execute(select(Motivo).where(Motivo.activo == True).order_by(Motivo.orden))  # noqa: E712
    motivos = [
        {"codigo": m.codigo, "descripcion": m.descripcion, "desambiguacion": m.desambiguacion}
        for m in result.scalars().all()
    ]
    return json.dumps({
        "motivo_reportado": motivo_reportado,
        "comentario": comentario,
        "catalogo_motivos": motivos,
        "instruccion": "Analiza si el comentario coincide con el motivo reportado. Si no coincide, sugiere el motivo correcto del catalogo.",
    })


async def _resumen_empresa(db: AsyncSession, empresa_id: int, actor: Actor = None) -> str:
    if not _actor_can_access(actor, empresa_id):
        return json.dumps({"error": "Fuera de tu alcance."})
    emp = (await db.execute(select(Empresa).where(Empresa.empresa_id == empresa_id))).scalar_one_or_none()
    if not emp:
        return json.dumps({"error": "Empresa no encontrada"})

    drivers_count = (await db.execute(select(func.count()).select_from(Driver).where(Driver.empresa_id == empresa_id, Driver.activo == True))).scalar_one()  # noqa: E712
    drivers_activated = (await db.execute(select(func.count()).select_from(Driver).where(Driver.empresa_id == empresa_id, Driver.activo == True, Driver.opted_in_at.isnot(None)))).scalar_one()  # noqa: E712
    vehicles_count = (await db.execute(select(func.count()).select_from(Vehicle).where(Vehicle.empresa_id == empresa_id, Vehicle.activo == True))).scalar_one()  # noqa: E712
    contactos_count = (await db.execute(select(func.count()).select_from(EmpresaContacto).where(EmpresaContacto.empresa_id == empresa_id, EmpresaContacto.activo == True))).scalar_one()  # noqa: E712

    return json.dumps({
        "empresa": emp.nombre, "region": emp.region, "comuna": emp.comuna,
        "conductores_total": drivers_count, "conductores_whatsapp": drivers_activated,
        "vehiculos": vehicles_count, "contactos": contactos_count,
    })


# ----------------------------------------------------------------------------
# CR-022 Part B — alerts tools
# ----------------------------------------------------------------------------

# Severity rank for ORDER BY. Higher = more urgent so we sort DESC.
_SEVERITY_RANK = {"baja": 0, "media": 1, "alta": 2, "critica": 3}


async def _listar_alertas_abiertas(  # noqa: PLR0912 -- scope branches are inherently per-actor-type
    db: AsyncSession,
    actor: Actor = None,
    empresa_id: int | None = None,
    dia_id: int | None = None,
    severity: str | None = None,
    tipo: str | None = None,
    limit: int = 20,
) -> str:
    """List open / notified alerts visible to `actor`.

    Scope rules:
      * falabella_admin / falabella_ops user → can see any empresa; honors
        explicit `empresa_id` if provided.
      * transport_manager / driver / contacto → restricted to their own
        empresa_id; if the LLM tries to pass a different `empresa_id` we
        override it (silent floor — the LLM may be confused, scope is
        non-negotiable).
      * actor=None → empty list (defensive).

    Output: { total, por_severity, items: [...] } where `por_severity`
    aggregates the *returned* rows (after limit) so the LLM can reason about
    the mix without a second tool call.
    """
    if actor is None:
        return json.dumps({"total": 0, "por_severity": {}, "items": [],
                           "warning": "Sin contexto de actor — no se puede listar"})

    # Clamp limit defensively (the JSON schema says max 100 but the LLM may
    # ignore it).
    limit = max(1, min(int(limit or 20), 100))

    stmt = select(Alert).where(Alert.estado.in_(("abierta", "notificada")))

    # Scope: falabella_* can pick any empresa; everyone else is pinned.
    if _actor_is_falabella_admin_or_ops(actor):
        if empresa_id is not None:
            stmt = stmt.where(Alert.empresa_id == empresa_id)
    else:
        default_eid = _actor_default_empresa_id(actor)
        if isinstance(actor, User):
            allowed = getattr(actor, "_empresa_ids", [])
            if not allowed:
                return json.dumps({"total": 0, "por_severity": {}, "items": []})
            stmt = stmt.where(Alert.empresa_id.in_(allowed))
            if empresa_id is not None and empresa_id in allowed:
                stmt = stmt.where(Alert.empresa_id == empresa_id)
        else:
            # Driver / contacto: hard-pinned to their empresa, regardless of
            # the empresa_id the LLM passed.
            if default_eid is None:
                return json.dumps({"total": 0, "por_severity": {}, "items": []})
            stmt = stmt.where(Alert.empresa_id == default_eid)

    if dia_id is not None:
        stmt = stmt.where(Alert.dia_id == dia_id)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    if tipo is not None:
        stmt = stmt.where(Alert.tipo == tipo)

    # Order: created_at DESC; we sort by severity in Python after fetch since
    # MSSQL doesn't have a portable CASE-based ORDER BY shortcut and we want
    # the same behavior on SQLite tests.
    stmt = stmt.order_by(Alert.created_at.desc()).limit(limit * 2)
    rows = (await db.execute(stmt)).scalars().all()

    rows_sorted = sorted(
        rows,
        key=lambda a: (_SEVERITY_RANK.get(a.severity, -1), a.created_at),
        reverse=True,
    )[:limit]

    items = [
        {
            "alert_id": a.alert_id,
            "tipo": a.tipo,
            "severity": a.severity,
            "estado": a.estado,
            "descripcion": a.descripcion,
            "dia_id": a.dia_id,
            "visita_id": a.visita_id,
            "empresa_id": a.empresa_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows_sorted
    ]

    por_severity: dict[str, int] = {}
    for a in rows_sorted:
        por_severity[a.severity] = por_severity.get(a.severity, 0) + 1

    return json.dumps({
        "total": len(items),
        "por_severity": por_severity,
        "items": items,
    })


async def _crear_alerta_manual(
    db: AsyncSession,
    actor: Actor = None,
    empresa_id: int | None = None,
    severity: str = "media",
    descripcion: str = "",
    dia_id: int | None = None,
    visita_id: int | None = None,
    auto_dispatch: bool = False,
) -> str:
    """Insert a manual alert on behalf of `actor`.

    Scope rules:
      * actor=None → reject (no caller context, can't attribute).
      * Driver / EmpresaContacto with no explicit `empresa_id` → default to
        actor.empresa_id.
      * Any actor → reject if `empresa_id` is outside scope (string error so
        the LLM can apologize to the user instead of crashing the chat turn).

    Dispatch rules: `auto_dispatch=True` only fans out when severity is
    `alta` or `critica`. Lower severities flag the alert as a "log-only"
    record so the operator can review at their leisure.
    """
    if actor is None:
        return json.dumps({"error": "Sin actor — no se puede crear alerta"})

    # Resolve empresa_id default for driver/contacto when LLM omitted it.
    if empresa_id is None:
        empresa_id = _actor_default_empresa_id(actor)
    if empresa_id is None:
        return json.dumps({"error": "empresa_id requerido"})

    if not _actor_can_access(actor, empresa_id):
        return json.dumps({
            "error": "Forbidden: el actor no tiene acceso a esa empresa",
            "empresa_id_solicitado": empresa_id,
        })

    if severity not in ("baja", "media", "alta", "critica"):
        return json.dumps({"error": f"severity invalido: {severity!r}"})

    descripcion = (descripcion or "").strip()
    if not descripcion:
        return json.dumps({"error": "descripcion requerida"})
    if len(descripcion) > 500:
        descripcion = descripcion[:500]

    alert = Alert(
        tipo="manual",
        severity=severity,
        empresa_id=empresa_id,
        dia_id=dia_id,
        visita_id=visita_id,
        descripcion=descripcion,
        estado="abierta",
        dedupe_key=None,  # manuals never dedupe — operator owns the call.
    )
    db.add(alert)
    await db.commit()
    await db.refresh(alert)

    dispatched = False
    recipients_count = 0
    if auto_dispatch and severity in ("alta", "critica"):
        # Imported lazily to avoid a top-level circular import (dispatcher
        # references Alert which references settings, etc).
        from app.core.alert_dispatcher import dispatch_alert

        result = await dispatch_alert(db, alert)
        dispatched = True
        recipients_count = result.sent
        await db.refresh(alert)

    return json.dumps({
        "alert_id": alert.alert_id,
        "estado": alert.estado,
        "severity": alert.severity,
        "empresa_id": alert.empresa_id,
        "dispatched": dispatched,
        "recipients_count": recipients_count,
    })


# ----------------------------------------------------------------------------
# CR-024 — cliente master tools (folio lookup + manual cancel)
# ----------------------------------------------------------------------------


async def _cliente_empresa_ids(db: AsyncSession, cliente_id: int) -> list[int]:
    """Return all distinct empresa_ids that have served `cliente_id`, computed
    live by joining ``visitas -> dias_operativos`` (CR-027: ``cliente_empresas``
    table is gone; the relationship is derived from operations).
    """
    ids = (
        await db.execute(
            select(DiaOperativo.empresa_id)
            .join(Visita, Visita.dia_id == DiaOperativo.dia_id)
            .where(Visita.cliente_id == cliente_id)
            .distinct()
        )
    ).scalars().all()
    return [int(x) for x in ids]


def _actor_visible_empresas(actor: Actor) -> set[int] | None:
    """Return the set of empresa_ids the actor is restricted to, or None when
    the actor is unrestricted (falabella_admin / falabella_ops).

    Returns an empty set if the actor has no visibility at all (defensive
    against transport_manager with no empresas).
    """
    if isinstance(actor, User):
        if actor.role in ("falabella_admin", "falabella_ops"):
            return None
        return set(getattr(actor, "_empresa_ids", []) or [])
    if isinstance(actor, Driver | EmpresaContacto):
        return {actor.empresa_id}
    return set()


async def _obtener_info_cliente_por_folio(
    db: AsyncSession,
    actor: Actor = None,
    folio_cliente: str = "",
) -> str:
    """Look up a cliente via a recent visita with `folio_cliente`. Returns the
    operational info the bot needs to advise the conductor.

    Scope: if the actor has restricted visibility, we only consider visitas
    whose `empresa_id` is in their allowed set; if no such visita exists, we
    return a generic "not found" so the bot doesn't leak a cliente from a
    different transportista.
    """
    folio = (folio_cliente or "").strip()
    if not folio:
        return json.dumps({"error": "folio_cliente requerido"})

    allowed = _actor_visible_empresas(actor)
    # Find the most recent visita with this folio (could be repeated across
    # multiple delivery orders for the same cliente).
    stmt = select(Visita).where(Visita.folio_cliente == folio)
    if allowed is not None:
        if not allowed:
            return json.dumps({"error": "Folio no encontrado en sus rutas"})
        stmt = stmt.where(Visita.empresa_id.in_(allowed))
    stmt = stmt.order_by(Visita.visita_id.desc()).limit(1)
    visita = (await db.execute(stmt)).scalar_one_or_none()
    if visita is None or visita.cliente_id is None:
        return json.dumps({"error": "Folio no encontrado en sus rutas"})

    cliente = (
        await db.execute(select(Cliente).where(Cliente.cliente_id == visita.cliente_id))
    ).scalar_one_or_none()
    if cliente is None:
        return json.dumps({"error": "Folio no encontrado en sus rutas"})

    # Double-check scope at the cliente_empresas level (a Driver may match a
    # visita in their empresa even when the cliente is also linked to others).
    if allowed is not None:
        cli_empresas = set(await _cliente_empresa_ids(db, cliente.cliente_id))
        if cli_empresas and not (cli_empresas & allowed):
            return json.dumps({"error": "Folio no encontrado en sus rutas"})

    # dias_no_disponible is stored as JSON text — surface as list to the LLM.
    dias = None
    if cliente.dias_no_disponible:
        try:
            parsed = json.loads(cliente.dias_no_disponible)
            if isinstance(parsed, list):
                dias = [str(x) for x in parsed]
        except (ValueError, TypeError):
            dias = None

    return json.dumps({
        "cliente_id": cliente.cliente_id,
        "nombre": cliente.nombre,
        "telefono": cliente.telefono,
        "es_vip": bool(cliente.es_vip),
        "vip_razon": cliente.vip_razon,
        "notas_operativas": cliente.notas_operativas,
        "direccion_default": cliente.direccion_default,
        "comuna_default": cliente.comuna_default,
        "ventana_horaria_inicio": (
            cliente.ventana_horaria_inicio.strftime("%H:%M")
            if cliente.ventana_horaria_inicio else None
        ),
        "ventana_horaria_fin": (
            cliente.ventana_horaria_fin.strftime("%H:%M")
            if cliente.ventana_horaria_fin else None
        ),
        "dias_no_disponible": dias,
        "prioridad": cliente.prioridad,
    })


async def _cancelar_visita_manual(
    db: AsyncSession,
    actor: Actor = None,
    visita_id: int | None = None,
    motivo: str = "",
) -> str:
    """Cancel a specific visita. The actor MUST have scope for the visita's
    empresa_id; otherwise the call is rejected with an error the LLM can
    surface verbatim.
    """
    if visita_id is None:
        return json.dumps({"error": "visita_id requerido"})
    motivo_s = (motivo or "").strip()
    if not motivo_s:
        return json.dumps({"error": "motivo requerido"})

    visita = (
        await db.execute(select(Visita).where(Visita.visita_id == int(visita_id)))
    ).scalar_one_or_none()
    if visita is None:
        return json.dumps({"error": f"Visita {visita_id} no encontrada"})

    if not _actor_can_access(actor, visita.empresa_id):
        return json.dumps({
            "error": "Forbidden: el actor no tiene acceso a esa visita",
            "visita_id": int(visita_id),
        })

    if visita.estado not in ("pendiente", "en_camino"):
        return json.dumps({
            "error": f"Visita no cancelable en estado {visita.estado!r}",
            "visita_id": visita.visita_id,
            "estado": visita.estado,
        })

    visita.estado = "cancelado"
    visita.motivo = f"Cancelado: {motivo_s[:200]}"
    await db.commit()
    return json.dumps({"ok": True, "visita_id": visita.visita_id})


# ----------------------------------------------------------------------------
# Reports tool — role-scoped operational report over WhatsApp / web chat.
# ----------------------------------------------------------------------------


async def _obtener_reporte(
    db: AsyncSession, actor: Actor = None, empresa_id: int | None = None, rango_dias: int = 1,
) -> str:
    """Compact operational report for an empresa, scoped to the actor.

    Reuses the report aggregation that powers /api/v1/reports so the bot and the
    web report show the same numbers. Default is the latest día; `rango_dias`>1
    aggregates the most recent N días.
    """
    # Lazy import: the report aggregation lives in the API layer; importing it
    # at module top would invert the core→api layering and risk a cycle.
    from app.api.v1.reports import _aggregate
    from app.core.config import settings

    requested = empresa_id
    eid, err = _resolve_scope(actor, empresa_id)
    if err:
        return json.dumps({"error": err})
    if eid is None:
        return json.dumps({"error": "Indica de que empresa quieres el reporte."})

    n = max(1, min(int(rango_dias or 1), 60))
    dias = (await db.execute(
        select(DiaOperativo.dia_id, DiaOperativo.fecha)
        .where(DiaOperativo.empresa_id == eid)
        .order_by(DiaOperativo.fecha.desc())
        .limit(n)
    )).all()
    if not dias:
        return json.dumps({"empresa_id": eid, "info": "Sin dias operativos para esta empresa."})

    dia_ids = [d for d, _ in dias]
    fechas = sorted(str(f) for _, f in dias)
    totals, vip, on_time, _by_region, by_driver, by_motivo = await _aggregate(
        db, dia_ids, settings.alerts_grace_min
    )
    empresa_nombre = await db.scalar(select(Empresa.nombre).where(Empresa.empresa_id == eid))

    # Worst performer (success%) among drivers with a meaningful sample.
    cands = [d for d in by_driver if d.visitas >= 5 and d.success_pct is not None]
    peor = None
    if cands:
        w = min(cands, key=lambda d: d.success_pct)
        peor = {"nombre": w.nombre, "driver_id": w.driver_id,
                "success_pct": w.success_pct, "visitas": w.visitas}

    out = {
        "empresa": empresa_nombre, "empresa_id": eid,
        "dias": len(dia_ids), "desde": fechas[0], "hasta": fechas[-1],
        "visitas": totals.visitas, "entregado": totals.entregado,
        "no_entregado": totals.no_entregado, "success_pct": totals.success_pct,
        "on_time_pct": on_time.on_time_pct,
        "vip_entregado": vip.entregado, "vip_total": vip.visitas,
        "top_motivos": [{"motivo": m.motivo, "count": m.count} for m in by_motivo[:3]],
        "peor_conductor": peor,
    }
    # If the caller asked for a different empresa than they can see, the scope
    # was floored — tell the LLM so it doesn't mislabel this as the requested
    # empresa's report (data is correctly the actor's own).
    if requested is not None and requested != eid:
        out["aviso"] = (
            f"Solo tienes acceso a tu empresa ({empresa_nombre}); se ignoro la "
            f"empresa {requested} solicitada. Este reporte es de TU empresa."
        )
    return json.dumps(out, default=str)

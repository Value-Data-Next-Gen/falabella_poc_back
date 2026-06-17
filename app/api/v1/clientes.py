"""Clientes (B2C recipients) endpoints — CR-019, CR-023, refactored in CR-027.

  GET    /api/v1/clientes                                   paginated list
  POST   /api/v1/clientes                                   create
  GET    /api/v1/clientes/{cliente_id}                      detail
  GET    /api/v1/clientes/{cliente_id}/historial-visitas    visit history
  GET    /api/v1/clientes/{cliente_id}/empresas-servidas    derived projection
  GET    /api/v1/clientes/{cliente_id}/visitas-futuras      lookahead
  PATCH  /api/v1/clientes/{cliente_id}                      update master
  POST   /api/v1/clientes/{cliente_id}/cancel-pending-visitas
  DELETE /api/v1/clientes/{cliente_id}

CR-027 model — *the* difference from CR-023:
  A cliente is a pure identity entity. There is NO `cliente.empresa_id`, no
  `cliente_empresas` table, no `empresas_servidas` field on the response. The
  link between a cliente and an empresa is *always* derived live from the
  operational chain:

      empresa <- dias_operativos <- rutas <- visitas -> cliente

  Multi-tenancy implications:
    - `falabella_admin/ops` see every cliente (the master is global).
    - `transport_manager` sees a cliente iff at least one visita references it
      AND that visita's `dia.empresa_id` is in their `_empresa_ids` scope.

BREAKING CHANGE vs CR-023:
  * `ClienteOut.empresa_id` removed.
  * `ClienteOut.empresas_servidas` removed.
  * `ClienteDetail` schema removed (GET `/clientes/{id}` returns `ClienteOut`).
  * `ClienteEmpresaOut` schema removed.
  * `ClienteCreate.empresa_id` removed — POST no longer accepts an empresa_id.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from loguru import logger
from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.core.twilio_templates import alerta_motivo_sid
from app.core.whatsapp import send_whatsapp
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.session import get_db
from app.schemas.cliente import (
    CancelPendingVisitasRequest,
    CancelPendingVisitasResult,
    ClienteCreate,
    ClienteListResponse,
    ClienteOut,
    ClienteUpdate,
    ClienteVisitaHistorialItem,
    ClienteVisitaHistorialResponse,
    ClienteVisitaProgramadaItem,
    ClienteVisitasFuturasResponse,
    EmpresaServidaOut,
    RetenerRequest,
    RetenerResult,
)

router = APIRouter(prefix="/api/v1/clientes", tags=["clientes"])


# ── Helpers ─────────────────────────────────────────────────────────────────


def _require_can_write(user: User) -> None:
    if user.role not in ("falabella_admin", "falabella_ops", "transport_manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires admin/ops/manager role")


def _is_falabella(user: User) -> bool:
    return user.role in ("falabella_admin", "falabella_ops")


def _empresa_ids(user: User) -> list[int]:
    return getattr(user, "_empresa_ids", []) or []


async def _get_or_404(db: AsyncSession, cliente_id: int) -> Cliente:
    result = await db.execute(select(Cliente).where(Cliente.cliente_id == cliente_id))
    c = result.scalar_one_or_none()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cliente not found")
    return c


async def _user_can_see_cliente(
    db: AsyncSession, user: User, cliente_id: int
) -> bool:
    """A user can see a cliente iff falabella admin/ops, OR at least one visita
    references the cliente AND its dia.empresa_id is in their scope.
    """
    if _is_falabella(user):
        return True
    ids = _empresa_ids(user)
    if not ids:
        return False
    n = (
        await db.execute(
            select(func.count(Visita.visita_id))
            .join(DiaOperativo, DiaOperativo.dia_id == Visita.dia_id)
            .where(
                Visita.cliente_id == cliente_id,
                DiaOperativo.empresa_id.in_(ids),
            )
        )
    ).scalar_one()
    return int(n) > 0


async def _visitas_total_for(
    db: AsyncSession, user: User, cliente_ids: list[int]
) -> dict[int, int]:
    """Bulk COUNT(visitas) per cliente_id, scoped to caller. Returns map."""
    if not cliente_ids:
        return {}
    stmt = (
        select(Visita.cliente_id, func.count(Visita.visita_id))
        .join(DiaOperativo, DiaOperativo.dia_id == Visita.dia_id)
        .where(Visita.cliente_id.in_(cliente_ids))
        .group_by(Visita.cliente_id)
    )
    if not _is_falabella(user):
        ids = _empresa_ids(user)
        if not ids:
            return {cid: 0 for cid in cliente_ids}
        stmt = stmt.where(DiaOperativo.empresa_id.in_(ids))
    rows = (await db.execute(stmt)).all()
    out: dict[int, int] = {cid: 0 for cid in cliente_ids}
    for cid, n in rows:
        out[int(cid)] = int(n or 0)
    return out


def _project_cliente_out(c: Cliente, visitas_total: int) -> ClienteOut:
    base = ClienteOut.model_validate(c)
    base.visitas_total = visitas_total
    return base


# ── List ────────────────────────────────────────────────────────────────────


@router.get(
    "",
    operation_id="listClientes",
    response_model=ClienteListResponse,
    summary=(
        "List clientes (scoped via visitas -> dias_operativos for transport_manager). "
        "Paginated wrapper: {items,total,limit,offset}."
    ),
)
async def list_clientes(
    empresa_id: int | None = Query(
        default=None,
        description=(
            "Filter clientes that have been served by this empresa "
            "(joins via visitas -> dias_operativos)."
        ),
    ),
    es_vip: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="search in nombre / rut / email"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ClienteListResponse:
    # transport_manager with no empresa_ids → empty result.
    if not _is_falabella(user) and not _empresa_ids(user):
        return ClienteListResponse(items=[], total=0, limit=limit, offset=offset)

    # Build the scoped subquery of eligible cliente_ids (when needed). The
    # subquery returns DISTINCT cliente_ids so the outer COUNT and the data
    # SELECT both stay correct without duplicates.
    needs_scope = (not _is_falabella(user)) or (empresa_id is not None)
    if needs_scope:
        v_stmt = (
            select(Visita.cliente_id)
            .join(DiaOperativo, DiaOperativo.dia_id == Visita.dia_id)
            .where(Visita.cliente_id.is_not(None))
            .distinct()
        )
        if not _is_falabella(user):
            v_stmt = v_stmt.where(DiaOperativo.empresa_id.in_(_empresa_ids(user)))
        if empresa_id is not None:
            v_stmt = v_stmt.where(DiaOperativo.empresa_id == empresa_id)
        scope_subq = v_stmt.subquery()
        stmt = select(Cliente).where(
            Cliente.cliente_id.in_(select(scope_subq.c.cliente_id))
        )
        count_stmt = select(func.count(Cliente.cliente_id)).where(
            Cliente.cliente_id.in_(select(scope_subq.c.cliente_id))
        )
    else:
        stmt = select(Cliente)
        count_stmt = select(func.count(Cliente.cliente_id))

    if es_vip is not None:
        stmt = stmt.where(Cliente.es_vip.is_(es_vip))
        count_stmt = count_stmt.where(Cliente.es_vip.is_(es_vip))
    if q:
        like = f"%{q}%"
        cond = or_(
            Cliente.nombre.ilike(like),
            Cliente.rut.ilike(like),
            Cliente.email.ilike(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    stmt = stmt.order_by(Cliente.cliente_id.desc()).offset(offset).limit(limit)
    clientes = (await db.execute(stmt)).scalars().all()
    totals_map = await _visitas_total_for(
        db, user, [c.cliente_id for c in clientes]
    )

    items = [
        _project_cliente_out(c, totals_map.get(c.cliente_id, 0)) for c in clientes
    ]
    return ClienteListResponse(items=items, total=total, limit=limit, offset=offset)


# ── Create ──────────────────────────────────────────────────────────────────


@router.post(
    "",
    operation_id="createCliente",
    response_model=ClienteOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "rut already exists (cliente master is global)"},
    },
)
async def create_cliente(
    body: ClienteCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ClienteOut:
    """Create a cliente. The master is tenant-agnostic — a brand-new cliente has
    no visitas yet, so it has no derived empresa link until ingest connects it.

    Idempotency on RUT is enforced by the global filtered unique index created
    in CR-023 (rut IS NOT NULL UNIQUE). A duplicate RUT returns 409.
    """
    _require_can_write(user)

    cliente = Cliente(**body.model_dump())
    db.add(cliente)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        msg = str(e).lower()
        if (
            "uq_clientes_rut" in msg
            or "ix_clientes_rut_global" in msg
            or "unique" in msg
            or "duplicate" in msg
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"rut '{body.rut}' already exists (cliente is global)",
            ) from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"DB constraint: {e}") from None

    await db.commit()
    await db.refresh(cliente)
    # New cliente has no visitas yet → total=0.
    return _project_cliente_out(cliente, 0)


# ── Detail ──────────────────────────────────────────────────────────────────


@router.get(
    "/{cliente_id}",
    operation_id="getCliente",
    response_model=ClienteOut,
    responses={403: {"description": "Out of scope"}, 404: {"description": "Not found"}},
)
async def get_cliente(
    cliente_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ClienteOut:
    c = await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")
    totals = await _visitas_total_for(db, user, [cliente_id])
    return _project_cliente_out(c, totals.get(cliente_id, 0))


# ── CR-027 — Empresas servidas (derived projection) ─────────────────────────


@router.get(
    "/{cliente_id}/empresas-servidas",
    operation_id="getClienteEmpresasServidas",
    response_model=list[EmpresaServidaOut],
    summary=(
        "Derived list of empresas that have served this cliente, computed live "
        "from visitas. Scoped to caller's empresa_ids when transport_manager."
    ),
    responses={
        403: {"description": "Out of scope"},
        404: {"description": "Cliente not found"},
    },
)
async def get_cliente_empresas_servidas(
    cliente_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EmpresaServidaOut]:
    await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    stmt = (
        select(
            DiaOperativo.empresa_id,
            Empresa.nombre,
            func.count(Visita.visita_id).label("visitas_count"),
            func.min(Visita.created_at).label("first_at"),
            func.max(Visita.created_at).label("last_at"),
        )
        .join(DiaOperativo, DiaOperativo.dia_id == Visita.dia_id)
        .join(Empresa, Empresa.empresa_id == DiaOperativo.empresa_id)
        .where(Visita.cliente_id == cliente_id)
        .group_by(DiaOperativo.empresa_id, Empresa.nombre)
        .order_by(desc("last_at"))
    )
    if not _is_falabella(user):
        ids = _empresa_ids(user)
        if not ids:
            return []
        stmt = stmt.where(DiaOperativo.empresa_id.in_(ids))

    rows = (await db.execute(stmt)).all()
    return [
        EmpresaServidaOut(
            empresa_id=int(r[0]),
            empresa_nombre=r[1],
            visitas_count=int(r[2] or 0),
            first_at=r[3],
            last_at=r[4],
        )
        for r in rows
    ]


# ── Historial de visitas ────────────────────────────────────────────────────


@router.get(
    "/{cliente_id}/historial-visitas",
    operation_id="getClienteHistorialVisitas",
    response_model=ClienteVisitaHistorialResponse,
    responses={403: {"description": "Out of scope"}, 404: {"description": "Not found"}},
    summary=(
        "Visit history for a cliente, scoped to caller's empresa_ids "
        "(transport_manager sees only their own visitas of that cliente)."
    ),
)
async def get_cliente_historial_visitas(
    cliente_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ClienteVisitaHistorialResponse:
    # 404 if cliente doesn't exist.
    await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    base_filters = [Visita.cliente_id == cliente_id]
    if not _is_falabella(user):
        base_filters.append(Visita.empresa_id.in_(_empresa_ids(user)))

    count_stmt = select(func.count(Visita.visita_id)).where(*base_filters)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    # Outer-join Ruta + DiaOperativo + Empresa for the projection.
    stmt = (
        select(
            Visita.visita_id,
            Visita.dia_id,
            Visita.ruta_id,
            Visita.empresa_id,
            Visita.estado,
            Visita.motivo,
            Visita.eta_estimada,
            Visita.direccion,
            DiaOperativo.fecha,
            Ruta.folio,
            Empresa.nombre,
        )
        .outerjoin(DiaOperativo, DiaOperativo.dia_id == Visita.dia_id)
        .outerjoin(Ruta, Ruta.ruta_id == Visita.ruta_id)
        .outerjoin(Empresa, Empresa.empresa_id == Visita.empresa_id)
        .where(*base_filters)
        .order_by(desc(DiaOperativo.fecha), desc(Visita.visita_id))
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    items = [
        ClienteVisitaHistorialItem(
            visita_id=r[0],
            dia_id=r[1],
            ruta_id=r[2],
            empresa_id=r[3],
            estado=r[4],
            motivo=r[5],
            eta_estimada=r[6],
            direccion=r[7],
            fecha=r[8],
            ruta_folio=r[9],
            empresa_nombre=r[10],
        )
        for r in rows
    ]
    return ClienteVisitaHistorialResponse(
        items=items, total=total, limit=limit, offset=offset
    )


# ── Patch ───────────────────────────────────────────────────────────────────


_ACTIVE_DIA_STATES = ("BORRADOR", "VALIDADO", "EN_CURSO")
_ACTIVE_VISITA_STATES = ("pendiente", "en_camino")


async def _sync_es_vip_to_active_visitas(
    db: AsyncSession, cliente_id: int, new_value: bool
) -> int:
    """Bulk UPDATE the denormalized `visitas.es_vip` for every active visita
    of `cliente_id` in a non-closed day. Returns rowcount.

    Active = visita in pendiente/en_camino AND dia.estado in
    BORRADOR/VALIDADO/EN_CURSO. We intentionally avoid touching CERRADO days
    (historical record) and entregado/cancelado/no_entregado visitas (final).
    """
    active_dias = select(DiaOperativo.dia_id).where(
        DiaOperativo.estado.in_(_ACTIVE_DIA_STATES)
    )
    stmt = (
        update(Visita)
        .where(
            Visita.cliente_id == cliente_id,
            Visita.estado.in_(_ACTIVE_VISITA_STATES),
            Visita.dia_id.in_(active_dias),
        )
        # Visita.es_vip is Integer-backed; coerce so we don't depend on bool
        # serialization differences between SQLite and MSSQL.
        .values(es_vip=1 if new_value else 0)
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


@router.patch(
    "/{cliente_id}",
    operation_id="updateCliente",
    response_model=ClienteOut,
    summary=(
        "Update cliente master. CR-024: if `es_vip` changes, the new value is "
        "propagated to all active visitas (pendiente/en_camino in non-CERRADO "
        "days). `notas_operativas` changes are NOT denormalized — the LLM bot "
        "reads them live via `obtener_info_cliente_por_folio`."
    ),
)
async def update_cliente(
    cliente_id: int,
    body: ClienteUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ClienteOut:
    _require_can_write(user)
    c = await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    data = body.model_dump(exclude_unset=True)

    # CR-024 — detect sync-triggering changes BEFORE mutating the row.
    sync_visitas_count: int | None = None
    vip_will_change = "es_vip" in data and bool(data["es_vip"]) != bool(c.es_vip)
    new_vip_value = bool(data["es_vip"]) if vip_will_change else None

    # `dias_no_disponible` is stored as JSON-serialized text. Pydantic gives us
    # a `list[str]` (validated weekday codes); persist as JSON, NULL → NULL.
    if "dias_no_disponible" in data:
        codes = data["dias_no_disponible"]
        data["dias_no_disponible"] = json.dumps(codes) if codes is not None else None

    for k, v in data.items():
        setattr(c, k, v)
    c.updated_at = datetime.now(UTC)

    if vip_will_change:
        sync_visitas_count = await _sync_es_vip_to_active_visitas(
            db, cliente_id, bool(new_vip_value)
        )
        logger.info(
            f"[cliente {cliente_id}] VIP propagado a {sync_visitas_count} "
            f"visitas activas (new_value={new_vip_value})"
        )

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Conflict: {e}") from None
    await db.refresh(c)
    totals = await _visitas_total_for(db, user, [c.cliente_id])
    out = _project_cliente_out(c, totals.get(c.cliente_id, 0))
    # `sync_visitas_count` only appears when a propagating change actually
    # happened, so old clients keep their existing shape.
    if sync_visitas_count is not None:
        out.sync_visitas_count = sync_visitas_count
    return out


# ── CR-024 — Cancel pending visitas (bulk) ──────────────────────────────────


@router.post(
    "/{cliente_id}/cancel-pending-visitas",
    operation_id="cancelClientePendingVisitas",
    response_model=CancelPendingVisitasResult,
    summary=(
        "Bulk-cancel all pending / en_camino visitas of this cliente. Scope: "
        "`all` (default) | `today` | `next_n_days` (requires `dias`). Only "
        "affects visitas in BORRADOR/VALIDADO/EN_CURSO days."
    ),
    responses={
        400: {"description": "scope=next_n_days requires `dias`"},
        403: {"description": "Out of scope"},
        404: {"description": "Cliente not found"},
    },
)
async def cancel_pending_visitas(
    cliente_id: int,
    body: CancelPendingVisitasRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CancelPendingVisitasResult:
    _require_can_write(user)
    await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    if body.scope == "next_n_days" and body.dias is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "`dias` is required when scope=next_n_days",
        )

    today = datetime.now(UTC).date()

    # Build the dia_id subquery according to scope. We always restrict to
    # non-closed days so cancelling never rewrites history.
    dia_stmt = select(DiaOperativo.dia_id, DiaOperativo.empresa_id).where(
        DiaOperativo.estado.in_(_ACTIVE_DIA_STATES)
    )
    if body.scope == "today":
        dia_stmt = dia_stmt.where(DiaOperativo.fecha == today)
    elif body.scope == "next_n_days":
        upper = today + timedelta(days=body.dias or 0)
        dia_stmt = dia_stmt.where(
            DiaOperativo.fecha >= today, DiaOperativo.fecha <= upper
        )
    # transport_manager: restrict to dias of their empresas. Falabella admin/
    # ops see everything.
    if not _is_falabella(user):
        ids = _empresa_ids(user)
        if not ids:
            return CancelPendingVisitasResult(
                cancelled_count=0, dia_ids=[], visita_ids=[]
            )
        dia_stmt = dia_stmt.where(DiaOperativo.empresa_id.in_(ids))

    # Resolve dia_ids in scope.
    rows = (await db.execute(dia_stmt)).all()
    if not rows:
        return CancelPendingVisitasResult(
            cancelled_count=0, dia_ids=[], visita_ids=[]
        )
    dia_ids = sorted({r[0] for r in rows})

    # Select target visitas first so we can return their ids (for cache
    # invalidation upstream). Update is a separate statement because RETURNING
    # is not portable across SQLite and MSSQL on UPDATE.
    target_stmt = select(Visita.visita_id).where(
        Visita.cliente_id == cliente_id,
        Visita.estado.in_(_ACTIVE_VISITA_STATES),
        Visita.dia_id.in_(dia_ids),
    )
    visita_ids = [int(x) for x in (await db.execute(target_stmt)).scalars().all()]
    if not visita_ids:
        return CancelPendingVisitasResult(
            cancelled_count=0, dia_ids=dia_ids, visita_ids=[]
        )

    motivo_text = f"Cancelado por cliente: {body.motivo[:100]}"
    upd = (
        update(Visita)
        .where(Visita.visita_id.in_(visita_ids))
        .values(
            estado="cancelado",
            motivo=motivo_text,
            motivo_comentario="Cancelado desde master cliente",
        )
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(upd)
    await db.commit()
    cancelled = int(result.rowcount or len(visita_ids))
    logger.info(
        f"[cliente {cliente_id}] cancel-pending scope={body.scope} "
        f"days={body.dias} → {cancelled} visitas en {len(dia_ids)} dias"
    )
    return CancelPendingVisitasResult(
        cancelled_count=cancelled, dia_ids=dia_ids, visita_ids=visita_ids
    )


# ── CR-024 — Visitas futuras (lookahead) ────────────────────────────────────


@router.get(
    "/{cliente_id}/visitas-futuras",
    operation_id="getClienteVisitasFuturas",
    response_model=ClienteVisitasFuturasResponse,
    summary=(
        "List pendiente/en_camino visitas for this cliente in the next N days "
        "(default 7). Restricted to BORRADOR/VALIDADO/EN_CURSO days. Scoped to "
        "caller's empresa_ids when transport_manager."
    ),
    responses={
        403: {"description": "Out of scope"},
        404: {"description": "Cliente not found"},
    },
)
async def get_cliente_visitas_futuras(
    cliente_id: int,
    days: int = Query(default=7, ge=1, le=60),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ClienteVisitasFuturasResponse:
    await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    today = datetime.now(UTC).date()
    upper = today + timedelta(days=days)

    filters = [
        Visita.cliente_id == cliente_id,
        Visita.estado.in_(_ACTIVE_VISITA_STATES),
        DiaOperativo.estado.in_(_ACTIVE_DIA_STATES),
        DiaOperativo.fecha >= today,
        DiaOperativo.fecha <= upper,
    ]
    if not _is_falabella(user):
        ids = _empresa_ids(user)
        if not ids:
            return ClienteVisitasFuturasResponse(
                items=[], total=0, dias_lookahead=days
            )
        filters.append(Visita.empresa_id.in_(ids))

    stmt = (
        select(
            Visita.visita_id,
            Visita.dia_id,
            Visita.ruta_id,
            Visita.empresa_id,
            Visita.estado,
            Visita.eta_estimada,
            Visita.direccion,
            Visita.comuna,
            DiaOperativo.fecha,
            DiaOperativo.estado,
            Ruta.folio,
            Empresa.nombre,
        )
        .join(DiaOperativo, DiaOperativo.dia_id == Visita.dia_id)
        .outerjoin(Ruta, Ruta.ruta_id == Visita.ruta_id)
        .outerjoin(Empresa, Empresa.empresa_id == Visita.empresa_id)
        .where(*filters)
        # NOTE: avoid `nullslast()` — MSSQL doesn't accept the SQL standard
        # `NULLS LAST` clause. NULL ETAs end up first by default on MSSQL ASC,
        # which is acceptable for an operational lookahead (the UI surfaces
        # those visitas at the top so they're not silently buried).
        .order_by(DiaOperativo.fecha.asc(), Visita.eta_estimada.asc())
    )
    rows = (await db.execute(stmt)).all()
    items = [
        ClienteVisitaProgramadaItem(
            visita_id=r[0],
            dia_id=r[1],
            ruta_id=r[2],
            empresa_id=r[3],
            estado=r[4],
            eta_estimada=r[5],
            direccion=r[6],
            comuna=r[7],
            fecha=r[8],
            dia_estado=r[9],
            ruta_folio=r[10],
            empresa_nombre=r[11],
        )
        for r in rows
    ]
    return ClienteVisitasFuturasResponse(
        items=items, total=len(items), dias_lookahead=days
    )


# ── Delete ──────────────────────────────────────────────────────────────────


@router.delete(
    "/{cliente_id}",
    operation_id="deleteCliente",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard delete. Returns 409 if any visita references this cliente.",
    responses={409: {"description": "Cliente has visitas associated"}},
)
async def delete_cliente(
    cliente_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_can_write(user)
    await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    n_visitas = (
        await db.execute(
            select(func.count(Visita.visita_id)).where(Visita.cliente_id == cliente_id)
        )
    ).scalar_one()
    if int(n_visitas) > 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete: cliente has {n_visitas} visita(s) associated",
        )

    c = await _get_or_404(db, cliente_id)
    await db.delete(c)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── "No entregar" / retener ──────────────────────────────────────────────────

_RETENER_ESTADOS = ("pendiente", "en_camino")


async def _alert_drivers_no_entregar(db: AsyncSession, cliente: Cliente) -> tuple[int, int]:
    """For each pending visita of `cliente` in an EN_CURSO día, WhatsApp the
    assigned driver a CRITICA 'NO ENTREGAR' alert (reusing the approved
    ALERTA_MOTIVO template). Returns (visitas_afectadas, avisos_enviados)."""
    rows = (await db.execute(
        select(Visita.visita_id, Vehicle.plate, Driver.nombre, Driver.phone_e164, Driver.opted_in_at)
        .select_from(Visita)
        .join(DiaOperativo, Visita.dia_id == DiaOperativo.dia_id)
        .join(Ruta, Visita.ruta_id == Ruta.ruta_id, isouter=True)
        .join(Vehicle, Ruta.vehicle_id == Vehicle.vehicle_id, isouter=True)
        .join(Driver, Ruta.driver_id == Driver.driver_id, isouter=True)
        .where(
            Visita.cliente_id == cliente.cliente_id,
            Visita.estado.in_(_RETENER_ESTADOS),
            DiaOperativo.estado == "EN_CURSO",
        )
    )).all()

    sid = alerta_motivo_sid()
    motivo = (cliente.retener_motivo or "Cliente retenido").strip()[:200]
    sent = 0
    for _vid, plate, conductor, phone, opted_in in rows:
        if not phone or opted_in is None:
            continue  # driver not activated on WhatsApp
        ok = await send_whatsapp(
            to=phone,
            content_sid=sid,
            content_variables={
                "1": "CRITICA",
                "2": "NO ENTREGAR",
                "3": (plate or "-")[:20],
                "4": (conductor or "-")[:60],
                "5": (cliente.nombre or "-")[:60],
                "6": motivo or "-",
            },
        )
        if ok:
            sent += 1
    return len(rows), sent


@router.post(
    "/{cliente_id}/retener",
    operation_id="retenerCliente",
    response_model=RetenerResult,
    summary="Marcar/desmarcar 'No entregar' y avisar por WhatsApp al conductor.",
)
async def retener_cliente(
    cliente_id: int,
    body: RetenerRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> RetenerResult:
    _require_can_write(user)
    cliente = await _get_or_404(db, cliente_id)
    if not await _user_can_see_cliente(db, user, cliente_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    cliente.retener = body.retener
    cliente.retener_motivo = (body.motivo or None) if body.retener else None
    cliente.updated_at = datetime.now(UTC)
    await db.commit()

    visitas_afectadas = avisos = 0
    if body.retener and body.avisar_whatsapp:
        visitas_afectadas, avisos = await _alert_drivers_no_entregar(db, cliente)
        logger.info(
            f"[retener] cliente {cliente_id} retenido — visitas={visitas_afectadas} avisos={avisos}"
        )

    return RetenerResult(
        cliente_id=cliente_id, retener=cliente.retener,
        visitas_afectadas=visitas_afectadas, avisos_enviados=avisos,
    )

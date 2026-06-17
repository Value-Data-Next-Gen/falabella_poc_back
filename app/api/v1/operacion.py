"""Day lifecycle + rutas + visitas endpoints.

Multi-tenancy:
  Every handler that touches a `dia_id` / `ruta_id` / `visita_id` loads the
  parent `DiaOperativo` row and verifies `user` can access `dia.empresa_id`
  via `_check_dia_scope`. Listings of dias are pre-filtered via the
  `apply_scope` helper on `DiaOperativo.empresa_id`. Cross-tenant injection
  attempts return 403 (not 404) to discourage probing.

  See CR-021 audit findings; v1 of this router accepted cross-tenant POSTs
  silently. See `backend/tests/integration/test_operacion_scope.py`.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_visita_evento
from app.core.security import current_user
from app.core.security.scope import apply_scope, can_access_empresa
from app.core.twilio_templates import alerta_motivo_sid
from app.core.whatsapp import send_whatsapp
from app.db.models.alert import Alert
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.driver_position import DriverPosition
from app.db.models.motivo import Motivo
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento
from app.db.session import get_db
from app.schemas.dia_operativo import (
    DiaCreate,
    DiaOut,
    RutaCreate,
    RutaOut,
    VisitaCreate,
    VisitaOut,
    VisitaUpdate,
)
from app.schemas.visita_evento import (
    PlanEtasResult,
    PlanEtasWarning,
    PromoteVipsResult,
    VisitaCancelIn,
    VisitaEventoOut,
    VisitaMoveRouteIn,
    VisitaOrdenIn,
)


class DriverPositionIn(BaseModel):
    driver_id: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading: float | None = Field(default=None, ge=0, le=360)
    speed: float | None = Field(default=None, ge=0)
    accuracy: float | None = None
    visita_id: int | None = None


class DriverPositionOut(BaseModel):
    driver_id: str
    driver_nombre: str = ""
    ruta_id: int | None = None
    lat: float
    lon: float
    heading: float | None
    speed: float | None
    visita_id: int | None
    updated_at: datetime

router = APIRouter(prefix="/api/v1/operacion", tags=["operacion"])

TRANSITIONS = {
    "BORRADOR": ["VALIDADO"],
    "VALIDADO": ["EN_CURSO", "BORRADOR"],
    "EN_CURSO": ["CERRADO"],
    # CR-026: CERRADO → EN_CURSO permitido SOLO para falabella_admin (reabrir
    # un dia cerrado para demos/simulacion). El chequeo de rol se aplica en
    # `transition_dia`; las alertas auto-resueltas al cerrar NO se reactivan
    # (quedan como historial).
    "CERRADO": ["EN_CURSO"],
}

# Valid visita estados (whitelist for PATCH /visitas/{id}).
_VISITA_ESTADOS = {"pendiente", "en_camino", "entregado", "no_entregado", "cancelado"}


# ----------------------------------------------------------------------------
# Scope helpers (CR-021)
# ----------------------------------------------------------------------------

def _check_dia_scope(user: User, dia: DiaOperativo) -> None:
    """Raise 403 if `user` cannot access `dia.empresa_id`.

    Centralizes the tenant-isolation rule for every dia-scoped handler. Admin
    and ops bypass; transport_manager must have `dia.empresa_id` in their
    `user_empresas` junction.
    """
    if not can_access_empresa(user, dia.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")


async def _load_dia_or_404(db: AsyncSession, dia_id: int) -> DiaOperativo:
    dia = (await db.execute(select(DiaOperativo).where(DiaOperativo.dia_id == dia_id))).scalar_one_or_none()
    if not dia:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dia no encontrado")
    return dia


async def _load_dia_for_user(db: AsyncSession, dia_id: int, user: User) -> DiaOperativo:
    """Load a dia and verify the user can see it. 404 then 403."""
    dia = await _load_dia_or_404(db, dia_id)
    _check_dia_scope(user, dia)
    return dia


async def _load_visita_for_user(
    db: AsyncSession, visita_id: int, user: User
) -> tuple[Visita, DiaOperativo]:
    """Load a visita + its parent dia, enforcing tenant scope.

    Returns both because every CR-028 handler needs the dia's `estado` to
    decide whether the mutation is allowed (BORRADOR/VALIDADO/EN_CURSO).

    Raises:
        404 if the visita doesn't exist.
        403 if the user cannot access `dia.empresa_id`.
    """
    visita = (
        await db.execute(select(Visita).where(Visita.visita_id == visita_id))
    ).scalar_one_or_none()
    if visita is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visita no encontrada")
    dia = await _load_dia_for_user(db, visita.dia_id, user)
    return visita, dia


def _check_visita_mutable(dia: DiaOperativo) -> None:
    """Raise 400 if the parent dia is in a state where visitas are frozen.

    BORRADOR / VALIDADO / EN_CURSO allow operator mutations. CERRADO is
    immutable (and we never expect VALIDADO that loops back here unless an
    admin reopened — that path uses the dia transition).
    """
    if dia.estado not in ("BORRADOR", "VALIDADO", "EN_CURSO"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Día en estado {dia.estado!r}: no se permiten cambios sobre visitas",
        )


async def _dia_to_out(db: AsyncSession, dia: DiaOperativo) -> DiaOut:
    # Single conditional-aggregation query for the 3 visita counts (was 3
    # separate COUNTs) + 1 for rutas. For the LIST path use `_dias_to_out_batch`.
    row = (await db.execute(
        select(
            func.count(),
            func.sum(case((Visita.estado == "entregado", 1), else_=0)),
            func.sum(case((Visita.estado == "no_entregado", 1), else_=0)),
        ).where(Visita.dia_id == dia.dia_id)
    )).one()
    rutas_count = await db.scalar(select(func.count()).select_from(Ruta).where(Ruta.dia_id == dia.dia_id))
    out = DiaOut.model_validate(dia)
    out.rutas_count = int(rutas_count or 0)
    out.visitas_count = int(row[0] or 0)
    out.visitas_entregadas = int(row[1] or 0)
    out.visitas_no_entregadas = int(row[2] or 0)
    return out


async def _dias_to_out_batch(db: AsyncSession, dias: list[DiaOperativo]) -> list[DiaOut]:
    """Build DiaOut for many días in O(1) queries (3 total), not 4*N."""
    if not dias:
        return []
    ids = [d.dia_id for d in dias]
    vrows = (await db.execute(
        select(
            Visita.dia_id, func.count(),
            func.sum(case((Visita.estado == "entregado", 1), else_=0)),
            func.sum(case((Visita.estado == "no_entregado", 1), else_=0)),
        ).where(Visita.dia_id.in_(ids)).group_by(Visita.dia_id)
    )).all()
    vmap = {did: (int(t or 0), int(e or 0), int(n or 0)) for did, t, e, n in vrows}
    rrows = (await db.execute(
        select(Ruta.dia_id, func.count()).where(Ruta.dia_id.in_(ids)).group_by(Ruta.dia_id)
    )).all()
    rmap = {did: int(c or 0) for did, c in rrows}
    outs: list[DiaOut] = []
    for d in dias:
        t, e, n = vmap.get(d.dia_id, (0, 0, 0))
        out = DiaOut.model_validate(d)
        out.rutas_count = rmap.get(d.dia_id, 0)
        out.visitas_count, out.visitas_entregadas, out.visitas_no_entregadas = t, e, n
        outs.append(out)
    return outs


# ── Dias operativos ──

@router.get("/dias", operation_id="listDias", response_model=list[DiaOut])
async def list_dias(empresa_id: int | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[DiaOut]:
    stmt = select(DiaOperativo).order_by(DiaOperativo.fecha.desc())
    # CR-021: enforce tenant scope at the query level.
    stmt = apply_scope(stmt, user, DiaOperativo.empresa_id)
    if empresa_id is not None:
        # If a transport_manager explicitly requests another empresa, the
        # apply_scope filter still wins (its where-clause is AND-ed).
        stmt = stmt.where(DiaOperativo.empresa_id == empresa_id)
    result = await db.execute(stmt)
    return await _dias_to_out_batch(db, list(result.scalars().all()))


@router.post("/dias", operation_id="createDia", response_model=DiaOut, status_code=status.HTTP_201_CREATED)
async def create_dia(body: DiaCreate, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> DiaOut:
    if not can_access_empresa(user, body.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fuera de alcance")
    existing = await db.execute(select(DiaOperativo).where(DiaOperativo.empresa_id == body.empresa_id, DiaOperativo.fecha == body.fecha))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un dia operativo para esa empresa en {body.fecha}")
    dia = DiaOperativo(empresa_id=body.empresa_id, fecha=body.fecha, notas=body.notas, created_by_user_id=user.user_id)
    db.add(dia)
    await db.commit()
    await db.refresh(dia)
    return await _dia_to_out(db, dia)


@router.get("/dias/{dia_id}", operation_id="getDia", response_model=DiaOut)
async def get_dia(dia_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> DiaOut:
    dia = await _load_dia_for_user(db, dia_id, user)
    return await _dia_to_out(db, dia)


@router.post("/dias/{dia_id}/transition", operation_id="transitionDia", response_model=DiaOut)
async def transition_dia(dia_id: int, nuevo_estado: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> DiaOut:
    dia = await _load_dia_for_user(db, dia_id, user)
    allowed = TRANSITIONS.get(dia.estado, [])
    if nuevo_estado not in allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No se puede pasar de {dia.estado} a {nuevo_estado}. Permitidos: {allowed}")
    # CR-026: reapertura de dias CERRADO. Solo `falabella_admin` puede hacerlo;
    # capturamos esto ANTES de mutar el estado para no dejar el row inconsistente.
    is_reopen = dia.estado == "CERRADO" and nuevo_estado == "EN_CURSO"
    if is_reopen and user.role != "falabella_admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Solo falabella_admin puede reabrir días cerrados",
        )
    # Don't let a manual close silently abandon undelivered visitas — require
    # they be resolved/cancelled first. (The sim auto-close only fires when 0
    # pending remain, so it's unaffected.)
    if nuevo_estado == "CERRADO":
        pendientes = await db.scalar(
            select(func.count()).select_from(Visita).where(
                Visita.dia_id == dia_id, Visita.estado == "pendiente"
            )
        )
        if pendientes:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"No se puede cerrar el día con {pendientes} visita(s) pendiente(s); "
                "resuélvelas o cancélalas primero.",
            )
    now = datetime.now(UTC)
    dia.estado = nuevo_estado
    if is_reopen:
        # Limpiamos el timestamp de cierre. Las alertas auto-resueltas NO se
        # reactivan: quedan como historial del cierre previo.
        dia.cerrado_at = None
        logger.info(
            f"[transition] dia {dia_id} REABIERTO por user "
            f"{user.user_id} ({user.email})"
        )
    if nuevo_estado == "VALIDADO":
        dia.validado_at = now
    elif nuevo_estado == "EN_CURSO" and not is_reopen:
        dia.iniciado_at = now
    elif nuevo_estado == "CERRADO":
        dia.cerrado_at = now
        # CR-025: auto-resolve every pending alert tied to this dia so they
        # don't linger as zombies after the day is closed. We append a marker
        # to descripcion so operators can tell auto-resolves apart from manual
        # ones. Same transaction as the state flip — either both apply or
        # neither does.
        upd = (
            update(Alert)
            .where(
                Alert.dia_id == dia_id,
                Alert.estado.in_(("abierta", "notificada")),
            )
            .values(
                estado="resuelta",
                resolved_at=now,
                resolved_by_user_id=user.user_id,
                descripcion=Alert.descripcion + " [auto-resuelta: dia cerrado]",
            )
        )
        result = await db.execute(upd)
        auto_resolved = result.rowcount or 0
        if auto_resolved > 0:
            logger.info(
                f"[transition] dia {dia_id} CERRADO: "
                f"{auto_resolved} alertas auto-resueltas"
            )
    await db.commit()
    await db.refresh(dia)

    # CR-3b: push the end-of-day report to the empresa's contactos/usuarios.
    # Best-effort — never let a notification failure undo the close.
    if nuevo_estado == "CERRADO" and not is_reopen:
        try:
            from app.core.report_push import push_dia_report
            await push_dia_report(db, dia)
        except Exception:
            logger.exception(f"[transition] dia {dia_id} report push failed")

    return await _dia_to_out(db, dia)


@router.delete(
    "/dias/{dia_id}",
    operation_id="deleteDia",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "Out of scope or role not allowed to delete"},
        404: {"description": "Dia not found"},
        409: {"description": "Dia is not in BORRADOR; cannot delete"},
    },
)
async def delete_dia(
    dia_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a dia operativo.

    Allowed only if:
      * Caller is `falabella_admin` or a `transport_manager` whose scope
        includes `dia.empresa_id` (ops cannot delete).
      * `dia.estado == 'BORRADOR'` — anything past validation is immutable
        (returns 409).

    Cascade is explicit (no DB-level CASCADE existed pre-CR-021): we delete
    visitas of the dia, then rutas of the dia, then the dia row itself.
    Returns 204 No Content.
    """
    if user.role not in ("falabella_admin", "transport_manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo admin o transport_manager pueden eliminar dias")
    dia = await _load_dia_for_user(db, dia_id, user)
    if dia.estado != "BORRADOR":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Solo se pueden eliminar dias en BORRADOR (actual: {dia.estado})",
        )
    # Explicit cascade. DB-level CASCADE is added in migration 0021 too, but we
    # cannot rely on it during the window between deploy and migration run.
    await db.execute(delete(Visita).where(Visita.dia_id == dia_id))
    await db.execute(delete(Ruta).where(Ruta.dia_id == dia_id))
    await db.execute(delete(DiaOperativo).where(DiaOperativo.dia_id == dia_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Rutas ──

@router.get("/dias/{dia_id}/rutas", operation_id="listRutas", response_model=list[RutaOut])
async def list_rutas(dia_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[RutaOut]:
    await _load_dia_for_user(db, dia_id, user)
    rutas = list((await db.execute(
        select(Ruta).where(Ruta.dia_id == dia_id).order_by(Ruta.orden)
    )).scalars().all())
    if not rutas:
        return []
    # Batch the lookups (was driver + vehicle + count PER ruta = 3*N queries).
    driver_ids = {r.driver_id for r in rutas if r.driver_id}
    vehicle_ids = {r.vehicle_id for r in rutas if r.vehicle_id}
    names = dict((await db.execute(
        select(Driver.driver_id, Driver.nombre).where(Driver.driver_id.in_(driver_ids))
    )).all()) if driver_ids else {}
    plates = dict((await db.execute(
        select(Vehicle.vehicle_id, Vehicle.plate).where(Vehicle.vehicle_id.in_(vehicle_ids))
    )).all()) if vehicle_ids else {}
    counts = dict((await db.execute(
        select(Visita.ruta_id, func.count()).where(Visita.ruta_id.in_([r.ruta_id for r in rutas])).group_by(Visita.ruta_id)
    )).all())
    out_list = []
    for r in rutas:
        out = RutaOut.model_validate(r)
        out.driver_nombre = names.get(r.driver_id, "")
        out.vehicle_patente = plates.get(r.vehicle_id, "") if r.vehicle_id else None
        out.visitas_count = int(counts.get(r.ruta_id, 0))
        out_list.append(out)
    return out_list


@router.post("/dias/{dia_id}/rutas", operation_id="createRuta", response_model=RutaOut, status_code=status.HTTP_201_CREATED)
async def create_ruta(dia_id: int, body: RutaCreate, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> RutaOut:
    dia = await _load_dia_for_user(db, dia_id, user)
    if dia.estado != "BORRADOR":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Solo se pueden agregar rutas en estado BORRADOR")
    # CR-021: driver and vehicle must belong to the same empresa as the dia.
    driver = (await db.execute(select(Driver).where(Driver.driver_id == body.driver_id))).scalar_one_or_none()
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Driver no encontrado")
    if driver.empresa_id != dia.empresa_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El driver pertenece a otra empresa")
    if body.vehicle_id is not None:
        vehicle = (await db.execute(select(Vehicle).where(Vehicle.vehicle_id == body.vehicle_id))).scalar_one_or_none()
        if vehicle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehiculo no encontrado")
        if vehicle.empresa_id != dia.empresa_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "El vehiculo pertenece a otra empresa")
    ruta = Ruta(dia_id=dia_id, driver_id=body.driver_id, vehicle_id=body.vehicle_id, notas=body.notas)
    db.add(ruta)
    await db.commit()
    await db.refresh(ruta)
    out = RutaOut.model_validate(ruta)
    out.driver_nombre = driver.nombre
    return out


# ── Visitas ──

@router.get("/dias/{dia_id}/visitas", operation_id="listVisitas", response_model=list[VisitaOut])
async def list_visitas(dia_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[VisitaOut]:
    await _load_dia_for_user(db, dia_id, user)
    visitas = (await db.execute(
        select(Visita).where(Visita.dia_id == dia_id).order_by(Visita.ruta_id, Visita.orden)
    )).scalars().all()
    out = [VisitaOut.model_validate(v) for v in visitas]
    # Mark visitas whose cliente is flagged "No entregar" (one batched query).
    cids = {v.cliente_id for v in visitas if v.cliente_id}
    if cids:
        retained = set((await db.execute(
            select(Cliente.cliente_id).where(Cliente.cliente_id.in_(cids), Cliente.retener == True)  # noqa: E712
        )).scalars().all())
        for o in out:
            if o.cliente_id in retained:
                o.cliente_retener = True
    return out


@router.post("/dias/{dia_id}/visitas", operation_id="createVisita", response_model=VisitaOut, status_code=status.HTTP_201_CREATED)
async def create_visita(dia_id: int, body: VisitaCreate, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> VisitaOut:
    dia = await _load_dia_for_user(db, dia_id, user)
    if dia.estado not in ("BORRADOR", "VALIDADO"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Solo se pueden agregar visitas en BORRADOR o VALIDADO")
    visita = Visita(dia_id=dia_id, empresa_id=dia.empresa_id, ruta_id=body.ruta_id, **body.model_dump(exclude={"ruta_id"}))
    db.add(visita)
    await db.commit()
    await db.refresh(visita)
    return VisitaOut.model_validate(visita)


_WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_dias_no_disponible(raw: str | None) -> set[str]:
    """Decode the `clientes.dias_no_disponible` JSON-text column to a set.

    Same forgiving semantics as `app.schemas.cliente._parse_dias`: bad JSON →
    empty (don't crash the planner over a corrupted single row).
    """
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(x).lower() for x in parsed if str(x).lower() in _WEEKDAY_CODES}


@router.post(
    "/dias/{dia_id}/plan-etas",
    operation_id="planDiaEtas",
    response_model=PlanEtasResult,
)
async def plan_dia_etas(  # noqa: PLR0912, PLR0915
    dia_id: int,
    hora_inicio: int = 9,
    duracion_horas: int = 8,
    respetar_reglas_cliente: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> PlanEtasResult:
    """Distribute `eta_estimada` across each route's visitas within a shift window.

    Default mode (`respetar_reglas_cliente=false`) keeps the CR-019 behaviour:
    even spacing of `duracion_horas / N` between visitas, ordered by
    `visita.orden ASC` within each ruta.

    With `respetar_reglas_cliente=true` (CR-028) the planner additionally:

      * Looks up each visita's `cliente_id` to fetch `ventana_horaria_inicio`,
        `ventana_horaria_fin`, `dias_no_disponible`, `prioridad`.
      * If the cliente is unavailable on the dia's weekday (ISO code in
        `dias_no_disponible`), the visita is SKIPPED (eta_estimada untouched)
        and a `PlanEtasWarning` is appended. We do NOT auto-cancel — that's
        the operator's call via POST /visitas/{id}/cancel.
      * If the proposed ETA falls outside the cliente's window, it is clamped
        to `ventana_horaria_inicio` of that same date (UTC).
      * If `prioridad in (1, 2)` and the visita is the first of its ruta, the
        ETA is pulled to `shift_start` regardless of orden gap. This is a soft
        nudge; subsequent visitas still use the orden-based offset.

    Per affected visita we append `tipo='eta_recalc'` to the audit log with
    `{eta_old, eta_new}` and (when applicable) the rule that nudged it.
    """
    from datetime import timedelta

    dia = await _load_dia_for_user(db, dia_id, user)
    _check_visita_mutable(dia)

    shift_start = datetime.combine(dia.fecha, datetime.min.time()).replace(
        hour=hora_inicio, tzinfo=UTC
    )
    shift_end = shift_start + timedelta(hours=duracion_horas)
    total_seconds = duracion_horas * 3600
    dia_weekday = _WEEKDAY_CODES[dia.fecha.weekday()]

    rutas_result = await db.execute(select(Ruta).where(Ruta.dia_id == dia_id))
    total_assigned = 0
    warnings: list[PlanEtasWarning] = []

    # Cliente cache: avoid re-querying the same cliente when multiple visitas
    # share one (common in Falabella's catalogo grueso).
    cliente_cache: dict[int, Cliente | None] = {}

    async def _get_cliente(cid: int | None) -> Cliente | None:
        if cid is None:
            return None
        if cid in cliente_cache:
            return cliente_cache[cid]
        c = (
            await db.execute(select(Cliente).where(Cliente.cliente_id == cid))
        ).scalar_one_or_none()
        cliente_cache[cid] = c
        return c

    for ruta in rutas_result.scalars().all():
        visitas_result = await db.execute(
            select(Visita).where(Visita.ruta_id == ruta.ruta_id).order_by(Visita.orden)
        )
        ruta_visitas = visitas_result.scalars().all()
        if not ruta_visitas:
            continue
        gap = total_seconds / max(1, len(ruta_visitas))
        for idx, v in enumerate(ruta_visitas):
            # Persist the planning sequence as a dense 1..N `orden`. Previously
            # `orden` was left at its create-time default (0) for every visita,
            # so the ETA order and the stored `orden` disagreed and operator-
            # facing text rendered "Visita #0".
            v.orden = idx + 1
            proposed = shift_start + timedelta(seconds=gap * (idx + 1))
            old_eta = v.eta_estimada
            applied_rule: str | None = None

            if respetar_reglas_cliente:
                cliente = await _get_cliente(v.cliente_id)
                if cliente is not None:
                    # 1) Cliente unavailable on this weekday → skip + warn.
                    blocked = _parse_dias_no_disponible(cliente.dias_no_disponible)
                    if dia_weekday in blocked:
                        warnings.append(
                            PlanEtasWarning(
                                visita_id=v.visita_id,
                                reason=(
                                    f"Cliente no disponible los {dia_weekday} "
                                    f"({', '.join(sorted(blocked))}); ETA no asignada"
                                ),
                            )
                        )
                        continue
                    # 2) Window clamp.
                    vh_ini = cliente.ventana_horaria_inicio
                    vh_fin = cliente.ventana_horaria_fin
                    if vh_ini is not None:
                        window_start = datetime.combine(
                            dia.fecha, vh_ini
                        ).replace(tzinfo=UTC)
                        if proposed < window_start:
                            proposed = window_start
                            applied_rule = "clamp_to_window_inicio"
                    if vh_fin is not None:
                        window_end = datetime.combine(
                            dia.fecha, vh_fin
                        ).replace(tzinfo=UTC)
                        if proposed > window_end:
                            # Clamp back to window_start. If the natural
                            # spacing pushes us past the window, the planner
                            # cannot serve this visita within window; we
                            # park it at the window start and let the operator
                            # split the route.
                            proposed = window_start if vh_ini is not None else proposed
                            applied_rule = "clamp_to_window_fin"
                    # 3) High prioridad → first slot.
                    if (
                        cliente.prioridad in (1, 2)
                        and idx == 0
                        and proposed > shift_start
                    ):
                        proposed = shift_start
                        applied_rule = (
                            f"{applied_rule}+priority_first" if applied_rule
                            else "priority_first"
                        )
                    # Safety: never set an ETA past the shift_end + 1h slack.
                    if proposed > shift_end + timedelta(hours=1):
                        warnings.append(
                            PlanEtasWarning(
                                visita_id=v.visita_id,
                                reason="ETA fuera de turno luego de aplicar reglas",
                            )
                        )

            v.eta_estimada = proposed
            total_assigned += 1
            payload: dict[str, str | None] = {
                "eta_old": old_eta.isoformat() if old_eta else None,
                "eta_new": proposed.isoformat(),
            }
            if applied_rule is not None:
                payload["rule"] = applied_rule
            await log_visita_evento(
                db,
                visita_id=v.visita_id,
                tipo="eta_recalc",
                user_id=user.user_id,
                payload=payload,
            )

    await db.commit()
    return PlanEtasResult(
        dia_id=dia_id,
        visitas_planificadas=total_assigned,
        shift_start=shift_start.isoformat(),
        duracion_horas=duracion_horas,
        respetar_reglas_cliente=respetar_reglas_cliente,
        warnings=warnings,
    )


@router.get("/visitas/{visita_id}", operation_id="getVisita", response_model=VisitaOut)
async def get_visita(visita_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> VisitaOut:
    visita = (await db.execute(select(Visita).where(Visita.visita_id == visita_id))).scalar_one_or_none()
    if not visita:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visita no encontrada")
    # CR-021: scope check via parent dia.
    await _load_dia_for_user(db, visita.dia_id, user)
    return VisitaOut.model_validate(visita)


@router.patch("/visitas/{visita_id}", operation_id="updateVisita", response_model=VisitaOut)
async def update_visita(visita_id: int, body: VisitaUpdate, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> VisitaOut:
    visita = (await db.execute(select(Visita).where(Visita.visita_id == visita_id))).scalar_one_or_none()
    if not visita:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visita no encontrada")
    # CR-021: scope check via parent dia.
    dia = await _load_dia_for_user(db, visita.dia_id, user)
    # Don't allow edits on a closed día (its sibling action endpoints all guard;
    # this PATCH previously didn't, so a CERRADO día's visitas were mutable).
    _check_visita_mutable(dia)
    now = datetime.now(UTC)
    data = body.model_dump(exclude_unset=True)
    if data.get("estado") is not None and data["estado"] not in _VISITA_ESTADOS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"estado inválido: {data['estado']}. Permitidos: {sorted(_VISITA_ESTADOS)}",
        )
    if "estado" in data:
        if data["estado"] == "entregado":
            visita.completada_at = now
        elif data["estado"] == "en_camino":
            visita.llegada_at = now
    for k, v in data.items():
        setattr(visita, k, v)
    await db.commit()
    await db.refresh(visita)
    return VisitaOut.model_validate(visita)


# ── Visita actions (CR-028) ──


@router.patch(
    "/visitas/{visita_id}/orden",
    operation_id="reorderVisita",
    response_model=VisitaOut,
)
async def reorder_visita(
    visita_id: int,
    body: VisitaOrdenIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> VisitaOut:
    """Move a visita to a new position within its route.

    Shifts the neighbours by ±1 so the resulting `orden` sequence within the
    ruta remains a dense 1..N permutation. No-op if `nuevo_orden == orden`.

    Allowed in BORRADOR / VALIDADO / EN_CURSO. CERRADO → 400.
    """
    visita, dia = await _load_visita_for_user(db, visita_id, user)
    _check_visita_mutable(dia)
    if visita.ruta_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Visita sin ruta asignada: no se puede reordenar",
        )
    old_orden = visita.orden
    nuevo_orden = body.nuevo_orden
    if nuevo_orden == old_orden:
        # No-op but still record so the audit log shows the intent.
        await log_visita_evento(
            db,
            visita_id=visita.visita_id,
            tipo="orden_change",
            user_id=user.user_id,
            payload={"old_orden": old_orden, "nuevo_orden": nuevo_orden, "noop": True},
        )
        await db.commit()
        await db.refresh(visita)
        return VisitaOut.model_validate(visita)

    # Shift neighbours in the same ruta. We do this in two SQL UPDATEs to keep
    # the transaction terse — both run in the same session.
    if nuevo_orden > old_orden:
        await db.execute(
            update(Visita)
            .where(
                Visita.ruta_id == visita.ruta_id,
                Visita.visita_id != visita.visita_id,
                Visita.orden > old_orden,
                Visita.orden <= nuevo_orden,
            )
            .values(orden=Visita.orden - 1)
        )
    else:  # nuevo_orden < old_orden
        await db.execute(
            update(Visita)
            .where(
                Visita.ruta_id == visita.ruta_id,
                Visita.visita_id != visita.visita_id,
                Visita.orden >= nuevo_orden,
                Visita.orden < old_orden,
            )
            .values(orden=Visita.orden + 1)
        )
    visita.orden = nuevo_orden
    await log_visita_evento(
        db,
        visita_id=visita.visita_id,
        tipo="orden_change",
        user_id=user.user_id,
        payload={"old_orden": old_orden, "nuevo_orden": nuevo_orden},
    )
    await db.commit()
    await db.refresh(visita)
    return VisitaOut.model_validate(visita)


_TERMINAL_ESTADOS = ("entregado", "no_entregado", "cancelado")


@router.post(
    "/visitas/{visita_id}/cancel",
    operation_id="cancelVisita",
    response_model=VisitaOut,
)
async def cancel_visita(
    visita_id: int,
    body: VisitaCancelIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> VisitaOut:
    """Mark a visita as cancelled with a motivo from the official catalog.

    400 if `motivo_codigo` is not present (or inactive) in `td.motivos`.
    409 if the visita is already in a terminal estado (entregado/no_entregado/
    cancelado). 400 if the parent dia is CERRADO.
    """
    visita, dia = await _load_visita_for_user(db, visita_id, user)
    if dia.estado == "CERRADO":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Día cerrado: no se pueden cancelar visitas",
        )
    if visita.estado in _TERMINAL_ESTADOS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Visita en estado terminal ({visita.estado}): no se puede cancelar",
        )
    motivo = (
        await db.execute(
            select(Motivo).where(
                Motivo.codigo == body.motivo_codigo, Motivo.activo == True  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if motivo is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"motivo_codigo {body.motivo_codigo!r} no existe en el catálogo o está inactivo",
        )

    old_estado = visita.estado
    visita.estado = "cancelado"
    visita.motivo = body.motivo_codigo
    visita.motivo_comentario = body.comentario or "Cancelado por torre"
    await log_visita_evento(
        db,
        visita_id=visita.visita_id,
        tipo="cancelada",
        user_id=user.user_id,
        payload={
            "old_estado": old_estado,
            "motivo_codigo": body.motivo_codigo,
            "comentario": body.comentario,
        },
    )
    await db.commit()
    await db.refresh(visita)
    return VisitaOut.model_validate(visita)


@router.post(
    "/rutas/{ruta_id}/promote-vips",
    operation_id="promoteRutaVips",
    response_model=PromoteVipsResult,
)
async def promote_ruta_vips(
    ruta_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> PromoteVipsResult:
    """Renumber the pending visitas of a ruta so VIPs land at the head.

    Completed/cancelled visitas keep their historical `orden` (we want the
    timeline of what actually happened to remain stable). Pending visitas
    (estado in {pendiente, en_camino}) get a fresh dense ranking, VIPs first
    (preserving their relative `orden`), then non-VIPs.

    Returns the count of VIPs whose position moved + the total touched.
    """
    ruta = (
        await db.execute(select(Ruta).where(Ruta.ruta_id == ruta_id))
    ).scalar_one_or_none()
    if ruta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta no encontrada")
    dia = await _load_dia_for_user(db, ruta.dia_id, user)
    _check_visita_mutable(dia)

    pending_result = await db.execute(
        select(Visita)
        .where(
            Visita.ruta_id == ruta_id,
            Visita.estado.in_(("pendiente", "en_camino")),
        )
        .order_by(case((Visita.es_vip.is_(None), 1), else_=0), Visita.es_vip.desc(), Visita.orden.asc())
    )
    pending = list(pending_result.scalars().all())
    if not pending:
        return PromoteVipsResult(ruta_id=ruta_id, vips_promoted=0, visitas_reordered=0)

    # Find a base offset that won't collide with the completed/cancelled rows.
    # Strategy: use 1..N for pending after we shift the completed/cancelled to
    # >= N+1 (renumbered to their original order). Simpler approach: renumber
    # ALL pending starting at 1, and shift completed/cancelled to start at
    # len(pending)+1. This keeps a contiguous orden sequence.
    completed_result = await db.execute(
        select(Visita)
        .where(
            Visita.ruta_id == ruta_id,
            Visita.estado.notin_(("pendiente", "en_camino")),
        )
        .order_by(Visita.orden.asc())
    )
    completed = list(completed_result.scalars().all())

    # First pass: stash old ordens for vip-promoted detection + audit.
    old_orden_by_id = {v.visita_id: v.orden for v in pending}

    vips_promoted = 0
    visitas_reordered = 0
    for new_orden, v in enumerate(pending, start=1):
        old = old_orden_by_id[v.visita_id]
        if old != new_orden:
            visitas_reordered += 1
            if v.es_vip:
                vips_promoted += 1
            v.orden = new_orden
            await log_visita_evento(
                db,
                visita_id=v.visita_id,
                tipo="promoted_vip",
                user_id=user.user_id,
                payload={
                    "old_orden": old,
                    "new_orden": new_orden,
                    "es_vip": bool(v.es_vip),
                },
            )

    # Append completed visitas after pending. They keep relative order.
    base = len(pending)
    for offset, v in enumerate(completed, start=1):
        target = base + offset
        if v.orden != target:
            v.orden = target

    await db.commit()
    return PromoteVipsResult(
        ruta_id=ruta_id,
        vips_promoted=vips_promoted,
        visitas_reordered=visitas_reordered,
    )


@router.post(
    "/visitas/{visita_id}/move-route",
    operation_id="moveVisitaRoute",
    response_model=VisitaOut,
)
async def move_visita_route(
    visita_id: int,
    body: VisitaMoveRouteIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> VisitaOut:
    """Move a visita from its current ruta to another one in the SAME dia.

    Steps (single transaction):
      1. Validate scope of the visita (via parent dia).
      2. Load the destination ruta and assert it belongs to the same dia.
      3. Compute `nuevo_orden`: if omitted, append (max(orden)+1).
      4. In origin ruta: shift `orden` down for those past `old_orden`.
      5. In destination ruta: shift `orden` up for those at/after `nuevo_orden`.
      6. Mutate the visita.
      7. Audit `tipo='ruta_change'`.

    400 if either ruta is in a CERRADO dia. 400 if `nueva_ruta_id` belongs to
    another dia.
    """
    visita, dia = await _load_visita_for_user(db, visita_id, user)
    _check_visita_mutable(dia)

    if visita.ruta_id == body.nueva_ruta_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "La visita ya pertenece a esa ruta",
        )

    dest_ruta = (
        await db.execute(
            select(Ruta).where(Ruta.ruta_id == body.nueva_ruta_id)
        )
    ).scalar_one_or_none()
    if dest_ruta is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Ruta destino no encontrada"
        )
    if dest_ruta.dia_id != dia.dia_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "La ruta destino pertenece a otro día operativo",
        )

    old_ruta_id = visita.ruta_id
    old_orden = visita.orden

    # Default destination position: append after the current max.
    max_dest_orden = (
        await db.execute(
            select(func.max(Visita.orden)).where(Visita.ruta_id == dest_ruta.ruta_id)
        )
    ).scalar_one() or 0
    nuevo_orden = body.nuevo_orden if body.nuevo_orden is not None else max_dest_orden + 1
    # Clamp upper bound — inserting past max+1 is the same as max+1.
    nuevo_orden = min(nuevo_orden, max_dest_orden + 1)

    # Origin ruta: close the gap left by the moving visita.
    if old_ruta_id is not None:
        await db.execute(
            update(Visita)
            .where(
                Visita.ruta_id == old_ruta_id,
                Visita.visita_id != visita.visita_id,
                Visita.orden > old_orden,
            )
            .values(orden=Visita.orden - 1)
        )

    # Destination ruta: open the slot.
    await db.execute(
        update(Visita)
        .where(
            Visita.ruta_id == dest_ruta.ruta_id,
            Visita.visita_id != visita.visita_id,
            Visita.orden >= nuevo_orden,
        )
        .values(orden=Visita.orden + 1)
    )

    visita.ruta_id = dest_ruta.ruta_id
    visita.orden = nuevo_orden

    await log_visita_evento(
        db,
        visita_id=visita.visita_id,
        tipo="ruta_change",
        user_id=user.user_id,
        payload={
            "old_ruta_id": old_ruta_id,
            "new_ruta_id": dest_ruta.ruta_id,
            "old_orden": old_orden,
            "new_orden": nuevo_orden,
        },
    )
    await db.commit()
    await db.refresh(visita)
    return VisitaOut.model_validate(visita)


@router.get(
    "/visitas/{visita_id}/eventos",
    operation_id="listVisitaEventos",
    response_model=list[VisitaEventoOut],
)
async def list_visita_eventos(
    visita_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VisitaEventoOut]:
    """Return the audit trail for one visita, most recent first."""
    # Scope check via parent dia.
    await _load_visita_for_user(db, visita_id, user)
    result = await db.execute(
        select(VisitaEvento)
        .where(VisitaEvento.visita_id == visita_id)
        .order_by(VisitaEvento.created_at.desc(), VisitaEvento.evento_id.desc())
    )
    return [VisitaEventoOut.from_orm_row(r) for r in result.scalars().all()]


# ── Driver positions ──

@router.post("/driver-positions", operation_id="upsertDriverPosition", response_model=DriverPositionOut)
async def upsert_driver_position(body: DriverPositionIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> DriverPositionOut:
    # CR-021: ensure the driver being updated belongs to the user's tenant.
    driver = (await db.execute(select(Driver).where(Driver.driver_id == body.driver_id))).scalar_one_or_none()
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Driver no encontrado")
    if not can_access_empresa(user, driver.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Out of scope")

    existing = (await db.execute(select(DriverPosition).where(DriverPosition.driver_id == body.driver_id))).scalar_one_or_none()
    if existing:
        existing.lat = body.lat
        existing.lon = body.lon
        existing.heading = body.heading
        existing.speed = body.speed
        existing.accuracy = body.accuracy
        existing.visita_id = body.visita_id
        existing.updated_at = datetime.now(UTC)
        pos = existing
    else:
        pos = DriverPosition(**body.model_dump())
        db.add(pos)
    await db.commit()
    await db.refresh(pos)
    return DriverPositionOut(driver_id=pos.driver_id, driver_nombre=driver.nombre, lat=pos.lat, lon=pos.lon, heading=pos.heading, speed=pos.speed, visita_id=pos.visita_id, updated_at=pos.updated_at)


@router.get("/dias/{dia_id}/driver-positions", operation_id="listDriverPositions", response_model=list[DriverPositionOut])
async def list_driver_positions(dia_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[DriverPositionOut]:
    await _load_dia_for_user(db, dia_id, user)
    rutas_result = await db.execute(select(Ruta).where(Ruta.dia_id == dia_id))
    rutas = rutas_result.scalars().all()
    if not rutas:
        return []
    driver_ids = [r.driver_id for r in rutas]
    ruta_by_driver = {r.driver_id: r.ruta_id for r in rutas}
    positions = list((await db.execute(
        select(DriverPosition).where(DriverPosition.driver_id.in_(driver_ids))
    )).scalars().all())
    # Batch driver names (was one Driver SELECT per position).
    names = dict((await db.execute(
        select(Driver.driver_id, Driver.nombre).where(Driver.driver_id.in_(driver_ids))
    )).all())
    return [
        DriverPositionOut(
            driver_id=pos.driver_id, driver_nombre=names.get(pos.driver_id, ""),
            ruta_id=ruta_by_driver.get(pos.driver_id), lat=pos.lat, lon=pos.lon,
            heading=pos.heading, speed=pos.speed, visita_id=pos.visita_id, updated_at=pos.updated_at,
        )
        for pos in positions
    ]


# ----------------------------------------------------------------------------
# Notify the assigned driver about a late / unfulfilled delivery (operator-
# triggered, targeted). The automatic path is the eta_breach cron; this lets an
# operator ping the specific driver of a visita on demand.
# ----------------------------------------------------------------------------

class NotifyDriverRequest(BaseModel):
    motivo: str = Field(default="ATRASO EN ENTREGA", max_length=60)
    detalle: str | None = Field(default=None, max_length=200)


class NotifyDriverResult(BaseModel):
    sent: bool
    driver_id: str | None = None
    driver_nombre: str | None = None
    motivo: str
    info: str | None = None  # why it wasn't sent, when sent is False


@router.post("/visitas/{visita_id}/notify-driver", operation_id="notifyDriverVisita",
             response_model=NotifyDriverResult)
async def notify_driver_visita(
    visita_id: int,
    body: NotifyDriverRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> NotifyDriverResult:
    visita = (await db.execute(select(Visita).where(Visita.visita_id == visita_id))).scalar_one_or_none()
    if visita is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visita no encontrada")
    if not can_access_empresa(user, visita.empresa_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sin acceso a esta empresa")

    row = None
    if visita.ruta_id is not None:
        row = (await db.execute(
            select(Driver.driver_id, Driver.nombre, Driver.phone_e164, Driver.opted_in_at, Vehicle.plate)
            .select_from(Ruta)
            .join(Driver, Ruta.driver_id == Driver.driver_id, isouter=True)
            .join(Vehicle, Ruta.vehicle_id == Vehicle.vehicle_id, isouter=True)
            .where(Ruta.ruta_id == visita.ruta_id)
        )).first()
    if row is None or row[0] is None:
        return NotifyDriverResult(sent=False, motivo=body.motivo, info="Visita sin conductor asignado")

    did, nombre, phone, opted_in, plate = row
    if not phone or opted_in is None:
        return NotifyDriverResult(sent=False, driver_id=did, driver_nombre=nombre, motivo=body.motivo,
                                  info="Conductor sin WhatsApp activo")

    detalle = (body.detalle or
               f"Folio {visita.folio_cliente or '-'}, {visita.cliente_nombre}. Confirma el estado de la entrega.")
    ok = await send_whatsapp(
        to=phone, content_sid=alerta_motivo_sid(),
        content_variables={
            "1": "ALTA", "2": body.motivo[:60], "3": (plate or "-")[:20],
            "4": (nombre or "-")[:60], "5": (visita.cliente_nombre or "-")[:60], "6": detalle[:200],
        },
    )
    logger.info(f"[notify-driver] visita {visita_id} driver {did} sent={ok}")
    return NotifyDriverResult(sent=bool(ok), driver_id=did, driver_nombre=nombre, motivo=body.motivo,
                              info=None if ok else "Falló el envío por WhatsApp")

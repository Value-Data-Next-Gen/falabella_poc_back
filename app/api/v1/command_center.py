"""Centro de Control — a live, cross-empresa command board for operators running
many routes at once. Read-only aggregation over EN_CURSO días:

  * counters     — at-a-glance totals (rutas activas, pendientes, atrasadas, VIP, bloqueados, alertas)
  * routes       — per-route health (driver, progress, atrasadas, estado)
  * exceptions   — open alerts ranked by severity + age, enriched with cliente /
                   empresa / owner so an operator can triage and claim them.

Scoping: falabella_admin/ops see everything; transport_manager only their
empresas (via apply_scope). No predictive logic — everything is derived from
current state + the sim clock.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.core.security.scope import apply_scope
from app.db.models.alert import Alert
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/operacion", tags=["operacion"])

_PENDING = ("pendiente", "en_camino")
_SEV_RANK = {"baja": 0, "media": 1, "alta": 2, "critica": 3}


class Counters(BaseModel):
    rutas_activas: int
    visitas_pendientes: int
    atrasadas: int
    vip_pendientes: int
    bloqueados: int
    alertas_abiertas: int


class RouteHealth(BaseModel):
    ruta_id: int
    dia_id: int
    empresa_id: int
    empresa_nombre: str | None
    driver_id: str | None
    driver_nombre: str | None
    total: int
    entregadas: int
    no_entregadas: int
    pendientes: int
    atrasadas: int
    success_pct: float | None
    estado: str  # ok | en_riesgo | atrasada


class ExceptionItem(BaseModel):
    alert_id: int
    tipo: str
    severity: str
    descripcion: str
    empresa_id: int
    empresa_nombre: str | None
    dia_id: int | None
    visita_id: int | None
    cliente_nombre: str | None
    folio_cliente: str | None
    created_at: datetime | None
    edad_min: int | None
    estado: str
    owner_user_id: int | None
    owner_nombre: str | None


class CommandCenter(BaseModel):
    sim_now: datetime | None
    counters: Counters
    routes: list[RouteHealth]
    exceptions: list[ExceptionItem]


@router.get("/command-center", operation_id="getCommandCenter", response_model=CommandCenter)
async def get_command_center(  # noqa: PLR0915 -- one cohesive aggregation; splitting hurts readability
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CommandCenter:
    sim_now = await db.scalar(select(SimClock.sim_now).where(SimClock.id == 1)) or datetime.now(UTC)

    empresas = {eid: n for eid, n in (await db.execute(select(Empresa.empresa_id, Empresa.nombre))).all()}

    dias = (await db.execute(
        apply_scope(select(DiaOperativo.dia_id, DiaOperativo.empresa_id)
                    .where(DiaOperativo.estado == "EN_CURSO"), user, DiaOperativo.empresa_id)
    )).all()
    dia_ids = [d for d, _ in dias]
    dia_empresa = {d: e for d, e in dias}

    routes: list[RouteHealth] = []
    counters = Counters(rutas_activas=0, visitas_pendientes=0, atrasadas=0,
                        vip_pendientes=0, bloqueados=0, alertas_abiertas=0)

    if dia_ids:
        # estado counts per ruta
        est_rows = (await db.execute(
            select(Visita.ruta_id, Visita.dia_id, Visita.estado, func.count())
            .where(Visita.dia_id.in_(dia_ids)).group_by(Visita.ruta_id, Visita.dia_id, Visita.estado)
        )).all()
        # atrasadas per ruta (pendiente con ETA vencida)
        atr_rows = (await db.execute(
            select(Visita.ruta_id, func.count())
            .where(Visita.dia_id.in_(dia_ids), Visita.estado == "pendiente",
                   Visita.eta_estimada.isnot(None), Visita.eta_estimada < sim_now)
            .group_by(Visita.ruta_id)
        )).all()
        atr_by_ruta = {r: int(n) for r, n in atr_rows}
        # driver names per ruta
        ruta_rows = (await db.execute(
            select(Ruta.ruta_id, Ruta.dia_id, Ruta.driver_id, Driver.nombre)
            .join(Driver, Ruta.driver_id == Driver.driver_id, isouter=True)
            .where(Ruta.dia_id.in_(dia_ids))
        )).all()

        acc: dict = {}
        for ruta_id, dia_id, estado, n in est_rows:
            key = ruta_id
            acc.setdefault(key, {"dia_id": dia_id, "c": {}})
            acc[key]["c"][estado] = int(n)

        ruta_meta = {rid: (did, drv, nom) for rid, did, drv, nom in ruta_rows}
        seen = set()
        for rid, data in acc.items():
            if rid is None:
                continue
            c = data["c"]
            did = data["dia_id"]
            total = sum(c.values())
            ent = c.get("entregado", 0)
            noent = c.get("no_entregado", 0)
            pend = c.get("pendiente", 0) + c.get("en_camino", 0)
            atr = atr_by_ruta.get(rid, 0)
            term = ent + noent + c.get("cancelado", 0)
            _did_meta, drv, nom = ruta_meta.get(rid, (did, None, None))
            eid = dia_empresa.get(did, 0)
            estado_r = "atrasada" if atr > 0 else ("en_riesgo" if pend and ent + noent == 0 else "ok")
            routes.append(RouteHealth(
                ruta_id=rid, dia_id=did, empresa_id=eid, empresa_nombre=empresas.get(eid),
                driver_id=drv, driver_nombre=nom, total=total, entregadas=ent, no_entregadas=noent,
                pendientes=pend, atrasadas=atr, success_pct=(round(100 * ent / term, 1) if term else None),
                estado=estado_r,
            ))
            seen.add(rid)
        routes.sort(key=lambda r: (r.atrasadas, r.pendientes), reverse=True)

        # counters
        counters.rutas_activas = len(seen)
        counters.visitas_pendientes = (await db.scalar(
            select(func.count()).select_from(Visita).where(Visita.dia_id.in_(dia_ids), Visita.estado.in_(_PENDING))
        )) or 0
        counters.atrasadas = sum(atr_by_ruta.values())
        counters.vip_pendientes = (await db.scalar(
            select(func.count()).select_from(Visita).where(
                Visita.dia_id.in_(dia_ids), Visita.estado.in_(_PENDING), Visita.es_vip == 1)
        )) or 0
        counters.bloqueados = (await db.scalar(
            select(func.count()).select_from(Visita).join(Cliente, Visita.cliente_id == Cliente.cliente_id)
            .where(Visita.dia_id.in_(dia_ids), Visita.estado.in_(_PENDING), Cliente.retener == True)  # noqa: E712
        )) or 0

    # ---- exceptions: open alerts, ranked, enriched ----
    alerts = (await db.execute(
        apply_scope(select(Alert).where(Alert.estado.in_(("abierta", "notificada"))),
                    user, Alert.empresa_id).order_by(Alert.created_at.desc()).limit(200)
    )).scalars().all()
    counters.alertas_abiertas = len(alerts)

    # enrich: cliente/folio via visita; owner name via users
    vids = [a.visita_id for a in alerts if a.visita_id]
    vmap = {}
    if vids:
        vmap = {v.visita_id: v for v in (await db.execute(
            select(Visita).where(Visita.visita_id.in_(vids))
        )).scalars().all()}
    owner_ids = [a.owner_user_id for a in alerts if a.owner_user_id]
    omap = {}
    if owner_ids:
        omap = dict((await db.execute(
            select(User.user_id, User.display_name).where(User.user_id.in_(owner_ids))
        )).all())

    ranked = sorted(alerts, key=lambda a: (_SEV_RANK.get(a.severity, -1), a.created_at or datetime.min.replace(tzinfo=UTC)), reverse=True)[:50]
    exceptions = []
    for a in ranked:
        v = vmap.get(a.visita_id) if a.visita_id else None
        created = a.created_at
        edad = int((sim_now - created).total_seconds() // 60) if created else None
        exceptions.append(ExceptionItem(
            alert_id=a.alert_id, tipo=a.tipo, severity=a.severity, descripcion=a.descripcion,
            empresa_id=a.empresa_id, empresa_nombre=empresas.get(a.empresa_id),
            dia_id=a.dia_id, visita_id=a.visita_id,
            cliente_nombre=(v.cliente_nombre if v else None), folio_cliente=(v.folio_cliente if v else None),
            created_at=created, edad_min=edad, estado=a.estado,
            owner_user_id=a.owner_user_id, owner_nombre=omap.get(a.owner_user_id),
        ))

    return CommandCenter(sim_now=sim_now, counters=counters, routes=routes, exceptions=exceptions)

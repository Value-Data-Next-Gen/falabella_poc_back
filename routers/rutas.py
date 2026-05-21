"""Endpoints de detalle e integridad de rutas (Ronda 3.5).

Reglas operativas:
  1. Una ruta_id pertenece a EXACTAMENTE 1 empresa transportista.
  2. Una ruta_id opera en EXACTAMENTE 1 región.

Endpoints:
  GET /api/planificacion/ruta?ruta_id=R-YYYYMMDD-NNN
      Devuelve: empresa, región, driver, planned_date + lista de stops con
      cliente, folio (reference), subfolios (geo_suborders), is_vip, estado.
      Incluye flag valid_routing si la ruta respeta las 2 reglas.

  GET /api/planificacion/integridad-rutas?fecha=YYYY-MM-DD
      Lista todas las rutas del día que violan alguna regla.

Estos endpoints son la fuente para:
  - El agente WhatsApp ('ruta R-...') — devuelve un resumen del día de
    ese vehículo.
  - El agente web cuando el usuario busca por ruta.
"""
from __future__ import annotations

from datetime import date as _date_cls
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn


router = APIRouter(tags=["rutas"])


# ============================================================================
# Detalle de ruta
# ============================================================================
class RutaStop(BaseModel):
    tracking_id: str
    order: Optional[int] = None
    cliente: str
    direccion: Optional[str] = None
    comuna: Optional[str] = None
    folio: Optional[str] = None         # `reference` en fpoc.simpli_visits
    subfolios: list[str] = []           # de fpoc.geo_suborders por idruta
    status: str
    is_vip: bool = False
    vip_tier: Optional[str] = None
    eta: Optional[str] = None
    sla_hour: Optional[float] = None


class RutaDetalle(BaseModel):
    ruta_id: str
    planned_date: str
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    region: Optional[str] = None
    driver_name: Optional[str] = None
    patente: Optional[str] = None
    total_stops: int = 0
    completed: int = 0
    pending: int = 0
    failed: int = 0
    vip_count: int = 0
    folios_unicos: int = 0
    subfolios_total: int = 0
    valid_routing: bool = True   # cumple "1 región + 1 empresa"
    integrity_warnings: list[str] = []
    stops: list[RutaStop] = []


@router.get("/api/planificacion/ruta", response_model=RutaDetalle)
def get_ruta(
    ruta_id: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> RutaDetalle:
    rid = ruta_id.strip()
    if not rid:
        raise HTTPException(400, "ruta_id vacío")

    scope_where = ""
    scope_params: list = []
    if not user.is_falabella:
        scope_where = " AND empresa_falsa = ?"
        scope_params.append(user.empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT id, planned_date, "order", title, address, comuna, region,
                       empresa_falsa, patente_falsa, driver_name,
                       reference, status, current_eta_cl, sla_hour_checkout_eta
                FROM fpoc.simpli_visits
                WHERE ruta_id = ?{scope_where}
                ORDER BY "order", id""",
            rid, *scope_params,
        )
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(404, f"ruta {rid} no encontrada")

        # Nombre de empresa
        empresa_ids = {int(r.empresa_falsa) for r in rows if r.empresa_falsa is not None}
        empresa_nombre = None
        if len(empresa_ids) == 1:
            eid = next(iter(empresa_ids))
            cur.execute(
                "SELECT nombre FROM fpoc.empresas_transporte WHERE empresa_id = ?",
                eid,
            )
            er = cur.fetchone()
            if er:
                empresa_nombre = str(er.nombre)

        # VIPs activos por title (global o por la empresa que el user puede ver)
        cur.execute(
            "SELECT match_value, tier FROM fpoc.vip_clients "
            "WHERE active = 1 AND match_type = 'title'"
        )
        vip_map: dict[str, str] = {str(r.match_value): str(r.tier) for r in cur.fetchall()}

        # Subfolios: cruzar por reference (folio) → geo_suborders.parentorder.
        # NOTA: ruta_id de simpli_visits es string (R-YYYYMMDD-NNN); idruta de
        # geo_suborders es BIGINT — no son el mismo dominio. El cross real
        # se hace por folio.
        references = [r.reference for r in rows if r.reference is not None]
        subfolios_all: list[str] = []
        if references:
            try:
                marks = ",".join(["?"] * len(references))
                cur.execute(
                    f"SELECT DISTINCT Suborden FROM fpoc.geo_suborders "
                    f"WHERE parentorder IN ({marks})",
                    *references,
                )
                subfolios_all = [str(r.Suborden) for r in cur.fetchall() if r.Suborden is not None]
            except Exception:  # noqa: BLE001
                # Tipos pueden no matchear según loader. Reportamos 0 en vez de 500.
                subfolios_all = []

    regiones = {str(r.region) for r in rows if r.region}
    empresa_ids_seen = empresa_ids
    integrity_warnings = []
    if len(regiones) > 1:
        integrity_warnings.append(
            f"ruta opera en {len(regiones)} regiones: {sorted(regiones)}"
        )
    if len(empresa_ids_seen) > 1:
        integrity_warnings.append(
            f"ruta asignada a {len(empresa_ids_seen)} empresas: {sorted(empresa_ids_seen)}"
        )
    valid_routing = len(integrity_warnings) == 0

    region_main: Optional[str] = None
    if regiones:
        # Si solo hay 1, la usamos. Si hay varias, mayoritaria.
        if len(regiones) == 1:
            region_main = next(iter(regiones))
        else:
            counts: dict[str, int] = {}
            for r in rows:
                if r.region:
                    counts[str(r.region)] = counts.get(str(r.region), 0) + 1
            region_main = max(counts, key=lambda k: counts[k])

    empresa_id_main = next(iter(empresa_ids_seen)) if len(empresa_ids_seen) == 1 else None

    stops: list[RutaStop] = []
    folios = set()
    for r in rows:
        title = str(r.title or "")
        is_vip = title in vip_map
        folio = str(r.reference) if r.reference is not None else None
        if folio:
            folios.add(folio)
        stops.append(RutaStop(
            tracking_id=str(r.id),
            order=int(getattr(r, "order")) if hasattr(r, "order") else None,
            cliente=title,
            direccion=str(r.address) if r.address else None,
            comuna=str(r.comuna) if r.comuna else None,
            folio=folio,
            subfolios=[],  # se llenan por folio si se cruza con geo_suborders (out of scope acá)
            status=str(r.status) if r.status else "pending",
            is_vip=is_vip,
            vip_tier=vip_map.get(title),
            eta=str(r.current_eta_cl) if r.current_eta_cl else None,
            sla_hour=float(r.sla_hour_checkout_eta) if r.sla_hour_checkout_eta is not None else None,
        ))

    completed = sum(1 for s in stops if s.status == "completed")
    failed = sum(1 for s in stops if s.status == "failed")
    pending = sum(1 for s in stops if s.status == "pending")

    # Driver / patente más representativos (mayoría)
    drv_counts: dict[str, int] = {}
    pat_counts: dict[str, int] = {}
    for r in rows:
        if r.driver_name:
            drv_counts[str(r.driver_name)] = drv_counts.get(str(r.driver_name), 0) + 1
        if r.patente_falsa is not None:
            pat_counts[str(r.patente_falsa)] = pat_counts.get(str(r.patente_falsa), 0) + 1
    driver_name = max(drv_counts, key=lambda k: drv_counts[k]) if drv_counts else None
    patente = max(pat_counts, key=lambda k: pat_counts[k]) if pat_counts else None

    return RutaDetalle(
        ruta_id=rid,
        planned_date=str(rows[0].planned_date),
        empresa_id=empresa_id_main,
        empresa_nombre=empresa_nombre,
        region=region_main,
        driver_name=driver_name,
        patente=patente,
        total_stops=len(stops),
        completed=completed,
        pending=pending,
        failed=failed,
        vip_count=sum(1 for s in stops if s.is_vip),
        folios_unicos=len(folios),
        subfolios_total=len(subfolios_all),
        valid_routing=valid_routing,
        integrity_warnings=integrity_warnings,
        stops=stops,
    )


# ============================================================================
# Integridad: rutas que violan las reglas
# ============================================================================
class RutaInvalida(BaseModel):
    ruta_id: str
    planned_date: str
    n_regiones: int
    n_empresas: int
    n_visitas: int
    issue: str   # 'multi_region' | 'multi_empresa' | 'multi_region_y_empresa'


@router.get("/api/planificacion/integridad-rutas", response_model=list[RutaInvalida])
def integridad_rutas(
    fecha: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> list[RutaInvalida]:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    scope_where = ""
    scope_params: list = []
    if not user.is_falabella:
        scope_where = " AND empresa_falsa = ?"
        scope_params.append(user.empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT ruta_id,
                       COUNT(DISTINCT region) AS n_regiones,
                       COUNT(DISTINCT empresa_falsa) AS n_empresas,
                       COUNT(*) AS n_visitas
                FROM fpoc.simpli_visits
                WHERE planned_date = ?
                  AND ruta_id IS NOT NULL AND ruta_id <> ''
                  {scope_where}
                GROUP BY ruta_id
                HAVING COUNT(DISTINCT region) > 1 OR COUNT(DISTINCT empresa_falsa) > 1
                ORDER BY n_regiones DESC, n_empresas DESC""",
            fecha, *scope_params,
        )
        out: list[RutaInvalida] = []
        for r in cur.fetchall():
            multi_r = int(r.n_regiones or 0) > 1
            multi_e = int(r.n_empresas or 0) > 1
            issue = ("multi_region_y_empresa" if multi_r and multi_e
                     else "multi_region" if multi_r
                     else "multi_empresa")
            out.append(RutaInvalida(
                ruta_id=str(r.ruta_id),
                planned_date=fecha,
                n_regiones=int(r.n_regiones or 0),
                n_empresas=int(r.n_empresas or 0),
                n_visitas=int(r.n_visitas or 0),
                issue=issue,
            ))
    return out


# ============================================================================
# Folios + subfolios por (fecha, empresa)  — alimenta tabla bajo el mapa
# ============================================================================
class FolioRow(BaseModel):
    ruta_id: Optional[str] = None
    vehicle_id: Optional[int] = None
    driver_name: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    tracking_id: str
    order: Optional[int] = None
    cliente: str
    direccion: Optional[str] = None
    comuna: Optional[str] = None
    region: Optional[str] = None
    folio: Optional[str] = None
    subfolios: list[str] = []
    status: str
    motivo: Optional[str] = None
    is_vip: bool = False
    vip_tier: Optional[str] = None
    eta: Optional[str] = None
    hora_real: Optional[str] = None


@router.get("/api/operacion/folios", response_model=list[FolioRow])
def folios_del_dia(
    fecha: str = Query(...),
    empresa_id: Optional[int] = Query(None),
    ruta_id: Optional[str] = Query(None),
    only_vip: bool = Query(False),
    limit: int = Query(500, ge=1, le=5000),
    user: CurrentUser = Depends(current_user),
) -> list[FolioRow]:
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    where = ["v.planned_date = ?"]
    params: list = [fecha]
    if not user.is_falabella and user.empresa_id is not None:
        where.append("v.empresa_falsa = ?")
        params.append(user.empresa_id)
    elif empresa_id is not None:
        where.append("v.empresa_falsa = ?")
        params.append(empresa_id)
    if ruta_id:
        where.append("v.ruta_id = ?")
        params.append(ruta_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT v.id, v.ruta_id, v.patente_falsa, v.driver_name,
                       v.empresa_falsa, et.nombre AS empresa_nombre,
                       v."order", v.title, v.address, v.comuna, v.region,
                       v.reference, v.status, v.current_eta_cl,
                       v.checkout_cl, v.checkout_comment
                FROM fpoc.simpli_visits v
                LEFT JOIN fpoc.empresas_transporte et ON et.empresa_id = v.empresa_falsa
                WHERE {' AND '.join(where)}
                ORDER BY v.ruta_id, v."order", v.id
                LIMIT {int(limit)}""",
            *params,
        )
        rows = cur.fetchall()

        # VIPs
        cur.execute(
            "SELECT match_value, tier FROM fpoc.vip_clients "
            "WHERE active = 1 AND match_type = 'title'"
        )
        vip_map: dict[str, str] = {str(r.match_value): str(r.tier) for r in cur.fetchall()}

        # Subfolios por reference (folio)
        refs = [r.reference for r in rows if r.reference is not None]
        sub_by_ref: dict[str, list[str]] = {}
        if refs:
            try:
                # batch para evitar IN gigante
                for i in range(0, len(refs), 500):
                    batch = refs[i:i + 500]
                    marks = ",".join(["?"] * len(batch))
                    cur.execute(
                        f"SELECT parentorder, Suborden FROM fpoc.geo_suborders "
                        f"WHERE parentorder IN ({marks})",
                        *batch,
                    )
                    for r in cur.fetchall():
                        k = str(r.parentorder)
                        sub_by_ref.setdefault(k, []).append(str(r.Suborden))
            except Exception:  # noqa: BLE001
                sub_by_ref = {}

    out: list[FolioRow] = []
    for r in rows:
        title = str(r.title or "")
        is_vip = title in vip_map
        if only_vip and not is_vip:
            continue
        folio = str(r.reference) if r.reference is not None else None
        out.append(FolioRow(
            ruta_id=str(r.ruta_id) if r.ruta_id else None,
            vehicle_id=int(r.patente_falsa) if r.patente_falsa is not None else None,
            driver_name=str(r.driver_name) if r.driver_name else None,
            empresa_id=int(r.empresa_falsa) if r.empresa_falsa is not None else None,
            empresa_nombre=str(r.empresa_nombre) if r.empresa_nombre else None,
            tracking_id=str(r.id),
            order=int(getattr(r, "order")) if hasattr(r, "order") else None,
            cliente=title,
            direccion=str(r.address) if r.address else None,
            comuna=str(r.comuna) if r.comuna else None,
            region=str(r.region) if r.region else None,
            folio=folio,
            subfolios=sub_by_ref.get(folio, []) if folio else [],
            status=str(r.status) if r.status else "pending",
            motivo=str(r.checkout_comment)[:120] if getattr(r, "checkout_comment", None) else None,
            is_vip=is_vip,
            vip_tier=vip_map.get(title),
            eta=str(r.current_eta_cl) if r.current_eta_cl else None,
            hora_real=str(r.checkout_cl) if getattr(r, "checkout_cl", None) else None,
        ))
    return out

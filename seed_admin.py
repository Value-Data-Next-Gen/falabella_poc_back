"""Endpoint admin: generar visitas regionales bajo demanda.

POST /api/admin/seed/region-day
  Body: {fecha, regiones[], n_rutas_por_region?, n_stops_por_ruta?}
  Genera visitas sintéticas en estado pending para las regiones indicadas.
  ruta_id formato: R-SEED-YYYYMMDD-{REGCODE}-{NNN}
  Idempotente por (fecha, region): borra rutas SEED previas antes de insertar.

  Diseñado para complementar el XLSX real (que viene 100% RM). Permite
  cubrir el demo con multi-región sin contaminar la data del cliente.

GET /api/admin/seed/region-day/preview?fecha=&regiones=
  Devuelve qué se generaría (sin escribir). Para confirmar antes.
"""
from __future__ import annotations

import random
from datetime import date as _date_cls, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from auth import CurrentUser, require_admin
from db import get_conn

# Reuso del catálogo del seeder
from fpoc_loader.seed_regiones_estacionalidad import (
    REGIONES_DATA, DRIVER_POOL_REGIONES, CLIENTES_POOL, EMPRESAS_VALIDAS,
    _gen_visita_regiones, _next_visit_id, SIMPLI_INSERT_COLS,
)


router = APIRouter(prefix="/api/admin/seed", tags=["seed-admin"])


# Mapeo región → código corto para ruta_id (alineado con migrate_split)
REGION_TO_CODE = {
    "Valparaíso": "VPO",
    "Biobío": "BIO",
    "Bío-Bío": "BIO",
    "Araucanía": "ARA",
    "Coquimbo": "COQ",
    "Antofagasta": "ANT",
    "Maule": "MAU",
    "O'Higgins": "OHI",
    "RM": "RM",
    "Metropolitana": "RM",
}

# Regiones soportadas (las que existen en REGIONES_DATA)
SUPPORTED_REGIONS = sorted({r[1] for r in REGIONES_DATA})


class SeedRegionDayRequest(BaseModel):
    fecha: str = Field(..., description="YYYY-MM-DD")
    regiones: list[str] = Field(..., min_length=1, max_length=10)
    # Defaults piloto: regiones tienen MENOS demanda que Santiago. 1 ruta x 8
    # stops genera ~24 visitas para 3 regiones (vs 1800+ RM de XLSX típico).
    n_rutas_por_region: int = Field(default=1, ge=1, le=20)
    n_stops_por_ruta: int = Field(default=8, ge=1, le=80)


class SeedRegionDayResult(BaseModel):
    fecha: str
    regiones_procesadas: list[str]
    n_rutas_creadas: int
    n_visitas_creadas: int
    n_eliminadas_previas: int = 0
    sample_rutas: list[str] = []


def _validate_regiones(regiones: list[str]) -> list[tuple[str, str]]:
    """Devuelve [(region, code), ...]. Rechaza con 400 si alguna no es soportada."""
    out = []
    for r in regiones:
        r_clean = r.strip()
        code = REGION_TO_CODE.get(r_clean)
        if not code:
            raise HTTPException(
                400,
                f"Región no soportada: {r_clean!r}. "
                f"Válidas: {sorted(REGION_TO_CODE.keys())}",
            )
        out.append((r_clean, code))
    return out


def _gen_for_region(
    fecha: _date_cls, region: str, code: str,
    n_rutas: int, n_stops: int,
    start_visit_id: int, start_route_idx: int,
    rng: random.Random,
) -> list[tuple]:
    """Genera lista de tuplas listas para INSERT. status='pending' siempre."""
    # Comunas / addresses disponibles para la región
    region_rows = [r for r in REGIONES_DATA if r[1] == region]
    if not region_rows:
        raise HTTPException(400, f"REGIONES_DATA sin entradas para {region}")

    fecha_compact = fecha.strftime("%Y%m%d")
    # Fecha de inicio: 09:00 del día operativo
    fecha_inicio = datetime.combine(fecha, datetime.min.time()) + timedelta(hours=9)

    rows: list[tuple] = []
    visit_id = start_visit_id
    for ruta_n in range(n_rutas):
        ruta_idx = start_route_idx + ruta_n
        ruta_id = f"R-SEED-{fecha_compact}-{code}-{ruta_idx:03d}"
        drv = rng.choice(DRIVER_POOL_REGIONES)
        patente = rng.randint(100, 999)
        empresa_id = rng.choice(EMPRESAS_VALIDAS)

        for order in range(1, n_stops + 1):
            # Comuna + dirección random de la región
            comuna_row = rng.choice(region_rows)
            comuna, _, ct, addr_tpls = comuna_row
            street_tpl = rng.choice(addr_tpls)
            address = f"{street_tpl.replace('{n}', str(rng.randint(100, 9999)))} {comuna}"
            # Generar tupla con visita en pending (override en este punto)
            tup = _gen_visita_regiones(
                visit_id=visit_id,
                planned_date=fecha,
                ruta_id=ruta_id,
                order=order,
                empresa_id=empresa_id,
                drv=drv,
                patente=patente,
                fecha_inicio=fecha_inicio,
                comuna=comuna,
                region=region,
                ct=ct,
                address=address,
                is_failed=False,
            )
            # Forzar status='pending' (el helper devuelve 'completed' por default).
            # status es el índice 7 en la tupla — ver SIMPLI_INSERT_COLS.
            tup_list = list(tup)
            status_idx = SIMPLI_INSERT_COLS.index("status")
            tup_list[status_idx] = "pending"
            rows.append(tuple(tup_list))
            visit_id += 1
    return rows


@router.post("/region-day", response_model=SeedRegionDayResult)
def seed_region_day(
    req: SeedRegionDayRequest,
    user: CurrentUser = Depends(require_admin),
) -> SeedRegionDayResult:
    """Genera visitas sintéticas para 1+ regiones en una fecha dada."""
    try:
        fecha_obj = _date_cls.fromisoformat(req.fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {req.fecha}")

    regiones_validas = _validate_regiones(req.regiones)
    fecha_compact = fecha_obj.strftime("%Y%m%d")
    rng = random.Random(hash((req.fecha, tuple(sorted(req.regiones)), req.n_rutas_por_region)))

    n_visitas_total = 0
    n_rutas_total = 0
    n_eliminadas = 0
    sample_rutas: list[str] = []

    with get_conn() as cn:
        cur = cn.cursor()
        for region, code in regiones_validas:
            # Idempotencia: borrar rutas SEED previas de esta (fecha, region)
            prefix = f"R-SEED-{fecha_compact}-{code}-"
            cur.execute(
                "DELETE FROM fpoc.simpli_visits "
                "WHERE planned_date = ? AND ruta_id LIKE ?",
                req.fecha, prefix + "%",
            )
            deleted = cur.rowcount or 0
            n_eliminadas += deleted
            cn.commit()

            # Generar
            start_id = _next_visit_id(cn)
            rows = _gen_for_region(
                fecha=fecha_obj, region=region, code=code,
                n_rutas=req.n_rutas_por_region,
                n_stops=req.n_stops_por_ruta,
                start_visit_id=start_id,
                start_route_idx=1,
                rng=rng,
            )

            # Insert por chunks
            cols_sql = ", ".join(f"[{c}]" if not c.startswith('"') else c for c in SIMPLI_INSERT_COLS)
            placeholders = ", ".join(["?"] * len(SIMPLI_INSERT_COLS))
            cur.fast_executemany = True
            CHUNK = 500
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i + CHUNK]
                cur.executemany(
                    f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})",
                    chunk,
                )
            cn.commit()

            n_visitas_total += len(rows)
            n_rutas_total += req.n_rutas_por_region
            # Sample primera ruta
            if rows:
                first_ruta_id = rows[0][SIMPLI_INSERT_COLS.index("ruta_id")]
                sample_rutas.append(first_ruta_id)

        # Registrar / actualizar en planificacion_imports
        cur.execute(
            "SELECT 1 FROM fpoc.planificacion_imports WHERE fecha = ?",
            req.fecha,
        )
        if cur.fetchone():
            cur.execute(
                "UPDATE fpoc.planificacion_imports SET count = count + ?, "
                "imported_at = SYSDATETIME() WHERE fecha = ?",
                n_visitas_total, req.fecha,
            )
        else:
            cur.execute(
                "INSERT INTO fpoc.planificacion_imports "
                "(fecha, count, imported_by_user_id, state) "
                "VALUES (?, ?, ?, 'BORRADOR')",
                req.fecha, n_visitas_total, user.user_id,
            )
        cn.commit()

    logger.info(
        f"[seed-region-day] fecha={req.fecha} regiones={[r[0] for r in regiones_validas]} "
        f"creadas={n_visitas_total} eliminadas_previas={n_eliminadas}"
    )

    return SeedRegionDayResult(
        fecha=req.fecha,
        regiones_procesadas=[r[0] for r in regiones_validas],
        n_rutas_creadas=n_rutas_total,
        n_visitas_creadas=n_visitas_total,
        n_eliminadas_previas=n_eliminadas,
        sample_rutas=sample_rutas,
    )


class SupportedRegionsResponse(BaseModel):
    regiones: list[str]
    codes: dict[str, str]


@router.get("/regions-supported", response_model=SupportedRegionsResponse)
def get_supported_regions(_: CurrentUser = Depends(require_admin)) -> SupportedRegionsResponse:
    return SupportedRegionsResponse(
        regiones=SUPPORTED_REGIONS,
        codes=REGION_TO_CODE,
    )

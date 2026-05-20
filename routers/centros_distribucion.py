"""Centros de distribución por región.

Lectura simple. La creación/edición pasa por el script
`backend/scripts/seed_centros_distribucion.py` o por SQL directo — no se
expone API de mutación en este POC.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn


router = APIRouter(prefix="/api/centros-distribucion", tags=["centros-distribucion"])


class CentroDistribucion(BaseModel):
    cd_id: int
    region: str
    nombre: str
    ciudad: Optional[str] = None
    lat: float
    lon: float
    activo: bool = True


@router.get("", response_model=list[CentroDistribucion])
def list_cds(
    region: Optional[str] = None,
    user: CurrentUser = Depends(current_user),
) -> list[CentroDistribucion]:
    """Lista los CDs activos. Opcional filtro por region."""
    where = "WHERE activo = 1"
    params: list = []
    if region:
        where += " AND region = ?"
        params.append(region)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT cd_id, region, nombre, ciudad, lat, lon, activo "
            f"FROM fpoc.centros_distribucion {where} "
            f"ORDER BY region",
            *params,
        )
        rows = cur.fetchall()
    return [
        CentroDistribucion(
            cd_id=int(r.cd_id),
            region=str(r.region),
            nombre=str(r.nombre),
            ciudad=str(r.ciudad) if r.ciudad else None,
            lat=float(r.lat),
            lon=float(r.lon),
            activo=bool(r.activo),
        )
        for r in rows
    ]

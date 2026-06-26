"""Reference data endpoints — static lookups (no auth required)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.regiones_chile import REGIONES

router = APIRouter(prefix="/api/v1/reference", tags=["reference"])


@router.get(
    "/regiones",
    operation_id="listRegiones",
    summary="Regiones y comunas de Chile (ISO 3166-2:CL).",
)
async def list_regiones() -> list[dict]:
    return REGIONES

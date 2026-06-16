"""Admin endpoint to manually trigger the geocoding batch — CR-020.

Why this exists:
  After the demo on 2026-05-30 we discovered that the only ~25/2119 clientes
  had been geocoded by Nominatim before uvicorn restarted. The lifespan loop
  added in CR-020 fixes the recurring case, but admins still need a way to
  kick off a one-shot batch (e.g. right after a bulk re-ingest or after
  tuning Nominatim rate-limits) without waiting for the next interval.

Scope:
  POST /api/v1/admin/geocoding/run?empresa_id=X&dia_id=Y&max=200
  Returns counts + wall-clock duration.

Notes:
  * `dia_id` is accepted for forward compatibility (we may want to scope to a
    specific operational day later) but currently ONLY `empresa_id` is used,
    because clientes are not partitioned by dia. We resolve `dia_id` to its
    `empresa_id` if both are absent. Documented in the param description.
  * Hard cap `max <= 1000` so the call can't run forever. The lifespan loop
    keeps eating leftovers anyway.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.geocoding import geocode_pending_clientes
from app.core.security import require_admin
from app.db.models.dia_operativo import DiaOperativo
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/admin/geocoding", tags=["admin", "geocoding"])


class GeocodingRunResult(BaseModel):
    procesados: int = Field(description="Clientes inspected this run")
    ok: int = Field(description="Successfully resolved via Nominatim")
    fallback: int = Field(description="Left at comuna centroide for next pass")
    failed: int = Field(description="Marked 'failed' (reached max attempts)")
    duration_s: float = Field(description="Wall-clock seconds")


@router.post(
    "/run",
    operation_id="adminRunGeocoding",
    response_model=GeocodingRunResult,
    status_code=status.HTTP_200_OK,
    summary="Trigger a one-shot Nominatim batch over pending clientes (admin).",
    dependencies=[Depends(require_admin())],
    responses={
        403: {"description": "Only falabella_admin"},
        404: {"description": "dia_id provided but not found"},
    },
)
async def run_geocoding(
    empresa_id: int | None = Query(
        default=None,
        description="Scope to a single empresa. Omit to scan all pending across tenants.",
    ),
    dia_id: int | None = Query(
        default=None,
        description="If set, resolves to that dia's empresa_id. Cannot combine with empresa_id mismatch.",
    ),
    max: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Cap on clientes processed this run (Nominatim is 1 req/s).",
    ),
    db: AsyncSession = Depends(get_db),
) -> GeocodingRunResult:
    target_empresas: list[int] | None = None
    if dia_id is not None:
        dia = (
            await db.execute(select(DiaOperativo).where(DiaOperativo.dia_id == dia_id))
        ).scalar_one_or_none()
        if dia is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"dia_id={dia_id} not found")
        if empresa_id is not None and empresa_id != dia.empresa_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"empresa_id={empresa_id} does not match dia_id={dia_id} empresa={dia.empresa_id}",
            )
        target_empresas = [dia.empresa_id]
    elif empresa_id is not None:
        target_empresas = [empresa_id]

    t0 = time.perf_counter()
    report = await geocode_pending_clientes(empresa_ids=target_empresas, max_batch=max)
    duration_s = round(time.perf_counter() - t0, 3)

    return GeocodingRunResult(
        procesados=report["procesados"],
        ok=report["ok"],
        fallback=report["fallback"],
        failed=report["failed"],
        duration_s=duration_s,
    )

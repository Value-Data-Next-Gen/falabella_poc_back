"""Health + readiness endpoints.

  - /api/v1/health  — liveness (always 200 if process is up).
  - /api/v1/ready   — readiness (200 iff DB pool initialized + SELECT 1 OK).
"""
from __future__ import annotations

import subprocess

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app import __version__

router = APIRouter(prefix="/api/v1", tags=["system"])


class HealthResponse(BaseModel):
    status: str
    ready: bool
    version: str
    git_sha: str


class ReadyResponse(BaseModel):
    status: str
    reason: str | None = None


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


@router.get(
    "/health",
    operation_id="getHealth",
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def get_health(request: Request) -> HealthResponse:
    """Always 200 when the process is reachable. `ready` mirrors `/ready`."""
    db_ready = bool(getattr(request.app.state, "db_ready", False))
    return HealthResponse(
        status="ok",
        ready=db_ready,
        version=__version__,
        git_sha=_git_sha(),
    )


@router.get(
    "/ready",
    operation_id="getReady",
    response_model=ReadyResponse,
    summary="Readiness probe",
    responses={
        200: {"description": "Backend ready (DB pool up, SELECT 1 OK)."},
        503: {"description": "Not ready: DB pool down or initial ping failed."},
    },
)
async def get_ready(request: Request, response: Response) -> ReadyResponse:
    """Returns 200 iff DB pool initialized and `SELECT 1` succeeded at startup."""
    db_ready = bool(getattr(request.app.state, "db_ready", False))
    if db_ready:
        return ReadyResponse(status="ready")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="not_ready", reason="DB pool not initialized")

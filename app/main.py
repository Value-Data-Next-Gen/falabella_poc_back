"""FastAPI app entrypoint.

Boots uvicorn-ready app with:
  - lifespan: init DB pool, run `SELECT 1`, flip `app.state.db_ready`.
    CR-007 will add APScheduler crons here.
  - request_id middleware (logs + X-Request-Id header).
  - CORS allow-list from Settings.
  - Health + readiness routers.

Routers added in later CRs are wired here in alphabetical order.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api.v1 import (
    admin_geocoding,
    admin_users,
    alerts,
    auth,
    bulk_import,
    capacitaciones,
    chat,
    clientes,
    command_center,
    documents,
    drivers,
    empresa_contactos,
    empresas,
    health,
    ingest,
    mapa,
    motivos,
    onboarding,
    operacion,
    reference,
    reports,
    sim,
    twilio_webhook,
    vehicles,
)
from app.core.config import settings
from app.core.geocoding import geocode_pending_clientes_loop
from app.core.logging import request_id_middleware
from app.core.middleware import security_headers_middleware
from app.db.session import dispose_engine, ping_db
from app.jobs.alerts import setup_alert_jobs
from app.jobs.sim_progression import setup_sim_progression_job

# ----------------------------------------------------------------------------
# Lifespan
# ----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks. Extended in later CRs."""
    logger.info(f"Booting Torre de Control v{__version__}")
    logger.info(
        f"  DB target: {settings.db_server} / {settings.db_name} / schema {settings.db_schema}"
    )
    logger.info(
        f"  Twilio sender: {settings.twilio_whatsapp_from} (dry_run={settings.notifications_dry_run})"
    )

    # CR-003: initialize DB pool + ping.
    app.state.db_ready = False
    try:
        ok = await ping_db()
        app.state.db_ready = ok
        if ok:
            logger.info("DB pool ready (SELECT 1 OK)")
        else:
            logger.warning("DB pool created but SELECT 1 failed — /ready will return 503")
    except Exception as e:
        logger.error(f"DB pool init failed: {e}")
        app.state.db_ready = False

    # CR-022: APScheduler with the 3 alert crons (eta_breach, eta_preview,
    # vip_deadline). Off-switch via `settings.alerts_scheduler_enabled` so
    # one-shot deploys / tests can skip. Only started when DB is ready.
    app.state.scheduler = None
    if settings.alerts_scheduler_enabled and app.state.db_ready:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            scheduler = AsyncIOScheduler(timezone="UTC")
            setup_alert_jobs(scheduler)
            # CR-030: auto-progression of pendiente visitas as sim_now advances.
            setup_sim_progression_job(scheduler)
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info("APScheduler started with alert jobs + sim progression")
        except Exception as e:
            logger.error(f"Failed to start APScheduler: {e}")
    else:
        logger.info(
            "Alert scheduler skipped "
            f"(enabled={settings.alerts_scheduler_enabled}, db_ready={app.state.db_ready})"
        )

    # CR-020: long-lived geocoding loop. Idempotent — if uvicorn reloads us
    # mid-flight (--reload during dev) and the previous task still exists and
    # is alive, we do NOT spawn a duplicate. Only started when the DB is ready
    # to avoid hammering Nominatim while we can't even persist results.
    app.state.geocoding_task = None
    if app.state.db_ready:
        existing = getattr(app.state, "geocoding_task", None)
        if existing is None or existing.done():
            app.state.geocoding_task = asyncio.create_task(
                geocode_pending_clientes_loop(),
                name="geocode_pending_clientes_loop",
            )
            logger.info("Geocoding background loop scheduled")
        else:
            logger.info("Geocoding background loop already running — skipped")
    else:
        logger.warning("DB not ready — geocoding loop NOT started")

    yield

    # Shutdown — cancel the loop cleanly so uvicorn doesn't print
    # 'Task was destroyed but it is pending!' on exit.
    task = getattr(app.state, "geocoding_task", None)
    if task is not None and not task.done():
        task.cancel()
        # Loop catches everything except CancelledError, so suppressing both
        # is just defensive for unknown shutdown races.
        with suppress(asyncio.CancelledError, Exception):
            await task

    # CR-022: stop scheduler without waiting for running jobs.
    sched = getattr(app.state, "scheduler", None)
    if sched is not None:
        with suppress(Exception):
            sched.shutdown(wait=False)

    await dispose_engine()
    logger.info("Shutting down")


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

app = FastAPI(
    title="Torre de Control API",
    description="Falabella last-mile control tower — v2",
    version=__version__,
    openapi_url="/api/v1/openapi.json",
    docs_url=None,    # disabled in prod; CR-013 enables behind auth for internal users
    redoc_url=None,
    lifespan=lifespan,
)


# ----------------------------------------------------------------------------
# Middleware
# ----------------------------------------------------------------------------

# Request ID injection — runs first (innermost = first to execute on the way in,
# last on the way out, which is what we want for the response header).
app.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)

# Security headers on every response (HSTS, nosniff, X-Frame-Options, etc.).
app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


# ----------------------------------------------------------------------------
# Routers
# ----------------------------------------------------------------------------

# CR-002: health.
# CR-003: still health (now reads app.state.db_ready).
# CR-004+: auth, drivers, contactos, etc.
# StaticFiles for the SPA must be the LAST mount (added in CR-014).

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin_users.router)
app.include_router(empresas.router)
app.include_router(empresa_contactos.router)
app.include_router(vehicles.router)
app.include_router(drivers.router)
app.include_router(capacitaciones.router)
app.include_router(reference.router)
app.include_router(reports.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(motivos.router)
app.include_router(operacion.router)
app.include_router(sim.router)
app.include_router(twilio_webhook.router)
app.include_router(twilio_webhook.alias_router)  # back-compat: /api/twilio/inbound (Twilio Console path)
app.include_router(bulk_import.router)
app.include_router(clientes.router)
app.include_router(ingest.router)
app.include_router(admin_geocoding.router)
app.include_router(alerts.router)
app.include_router(mapa.router)
app.include_router(onboarding.router)
app.include_router(command_center.router)

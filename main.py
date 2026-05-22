"""ValueData backend (FastAPI) — bootstrap + lifespan + router registration.

Fase 2 MVP refactor: la fuente única de visitas es `fpoc.simpli_visits`.
El modelo ML XGBoost + SHAP + synthetic data generator se eliminó, junto con
los simuladores `comment_simulator` / `live_generator` / `driver_sim`.

Lo único que sigue corriendo en `lifespan` es:
  - migraciones idempotentes desde `fpoc_loader.migrations`
  - `STATE.init()` (carga maestros de DB; sin modelo ML)
  - `scheduler` con el VIP deadline cron (chequea fpoc.simpli_visits cada 60s)

Fase 3 reintroducirá un simulador de movimiento de drivers basado en
interpolación sobre `fpoc.simpli_visits` (sin synthetic data).
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from loguru import logger

# Cargar .env antes de importar state/auth (que leen DB_*)
for _p in (Path(__file__).resolve().parent / ".env",
           Path(__file__).resolve().parent.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from core.state import STATE
from core.auth import (
    empresas_router,
    router as auth_router,
)

logger.remove()
logger.add(sys.stderr, level="INFO")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Migraciones idempotentes con tracking en fpoc.schema_migrations.
    try:
        from fpoc_loader.migrations import MIGRATIONS, apply_all
        apply_all(MIGRATIONS)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[migrations] runner falló (se intenta seguir): {e}")

    logger.info("Bootstrapping ValueData backend (loading masters)...")
    STATE.init()
    logger.info(
        f"State ready. drivers={len(STATE.drivers)} "
        f"vehicles={len(STATE.vehicles_ext)} empresas={len(STATE.empresas)}"
    )

    scheduler = BackgroundScheduler()
    # VIP deadline checker (interval 60s) — lee de fpoc.simpli_visits
    from sims.vip_deadline_cron import register_cron as register_vip_cron
    register_vip_cron(scheduler)
    # Fase 3 MVP: ETA breach checker (interval 5min) — alerta automatica
    # WhatsApp si una visita pending pasa su ETA + GRACE_MINUTES.
    from sims.eta_breach_cron import register_cron as register_eta_breach_cron
    register_eta_breach_cron(scheduler)
    # Pieza #2: pre-aviso ETA (interval 5min) — recordatorio amistoso
    # 10-20 min ANTES de la ETA de la próxima visita.
    from sims.eta_preview_cron import register_cron as register_eta_preview_cron
    register_eta_preview_cron(scheduler)
    scheduler.start()
    logger.info("Scheduler started: VIP deadline + ETA breach + ETA preview crons")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="ValueData backend - Torre de Control",
    version="0.2.0",
    lifespan=lifespan,
)

_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# gzip para payloads grandes (plan-diario ~1.25 MB sin comprimir).
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

# ============================================================================
# Routers
# ============================================================================
from routers.seguimiento import router as seguimiento_router
from routers.notifications import router as notifications_router
from routers.preferences import router as preferences_router
from routers.vip import router as vip_router
from routers.priorities import router as priorities_router
from routers.plan_diario import router as plan_diario_router
from routers.watchlist import router as watchlist_router
from routers.mantenedores import router as mantenedores_router
from routers.me import router as me_router
from routers.comments import router as comments_router
from routers.empresa_contactos import router as empresa_contactos_router
from routers.motivo_classifier import router as motivo_classifier_router
from routers.motivo_corrections import router as motivo_corrections_router
from routers.drivers_whatsapp import router as drivers_whatsapp_router
from routers.day_planning import router as day_planning_router
from routers.day_state import router as day_state_router
from routers.rutas import router as rutas_router
from routers.seed_admin import router as seed_admin_router
from routers.search import router as search_router
from routers.twilio_inbound import router as twilio_inbound_router, _legacy_router as twilio_legacy_router
from routers.whatsapp_onboarding import router as whatsapp_onboarding_router
from routers.agent_web import router as agent_web_router
from routers.centros_distribucion import router as centros_distribucion_router
from routers.copiloto import router as copiloto_router
from routers.whatsapp_escalation import router as whatsapp_escalation_router
from routers.admin_invitations import router as admin_invitations_router
from routers.admin_day_notifications import router as admin_day_notifications_router
from routers.admin_day_stats import router as admin_day_stats_router
from routers.admin_interventions import router as admin_interventions_router
from routers.admin_pilot import router as admin_pilot_router
from routers.operacion import router as operacion_router
# Endpoints "legacy" sobrevivientes tras eliminar ML (system/fleet)
from routers.legacy_routes import (
    system_router,
    fleet_router,
)

app.include_router(auth_router)
app.include_router(empresas_router)
app.include_router(seguimiento_router)
app.include_router(notifications_router)
app.include_router(preferences_router)
app.include_router(vip_router)
app.include_router(priorities_router)
app.include_router(plan_diario_router)
app.include_router(watchlist_router)
app.include_router(mantenedores_router)
app.include_router(me_router)
app.include_router(comments_router)
app.include_router(empresa_contactos_router)
app.include_router(motivo_classifier_router)
app.include_router(motivo_corrections_router)
app.include_router(drivers_whatsapp_router)
app.include_router(day_planning_router)
app.include_router(day_state_router)
app.include_router(rutas_router)
app.include_router(seed_admin_router)
app.include_router(search_router)
app.include_router(twilio_inbound_router)
app.include_router(twilio_legacy_router)
app.include_router(whatsapp_onboarding_router)
app.include_router(agent_web_router)
app.include_router(centros_distribucion_router)
app.include_router(copiloto_router)
app.include_router(whatsapp_escalation_router)
app.include_router(admin_invitations_router)
app.include_router(admin_day_notifications_router)
app.include_router(admin_day_stats_router)
app.include_router(admin_interventions_router)
app.include_router(admin_pilot_router)
app.include_router(operacion_router)
app.include_router(system_router)
app.include_router(fleet_router)

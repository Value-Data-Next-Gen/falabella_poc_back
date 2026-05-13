"""ValueData backend (FastAPI) — bootstrap + lifespan + router registration.

Capa de predicción anticipada que se monta encima de SimpliRoute. En este POC
genera el plan localmente con la misma forma que devolvería SimpliRoute; en
producción `pipeline.gen_today_plan` se reemplaza por una llamada a la API real.

R7-F3: los 23 endpoints que vivían inline acá se movieron a `legacy_routes.py`
agrupados en 4 sub-routers (system / control / model / fleet). Este módulo
queda solo con bootstrap del FastAPI app, scheduler, lifespan e include_router.
Las URLs públicas no cambiaron.
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
from loguru import logger

# Cargar .env antes de importar state/auth (que leen DB_*)
for _p in (Path(__file__).resolve().parent / ".env",
           Path(__file__).resolve().parent.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from state import STATE
from auth import (
    empresas_router,
    router as auth_router,
)

logger.remove()
logger.add(sys.stderr, level="INFO")

SCHEDULER_TICK_SEC = 3  # cada 3s avanza sim_clock por sim_minutes_per_tick


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Migraciones idempotentes con tracking en fpoc.schema_migrations.
    # Se corren antes de STATE.init() porque train_model lee fpoc_simpli_visits.
    try:
        from fpoc_loader.migrations import MIGRATIONS, apply_all
        apply_all(MIGRATIONS)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[migrations] runner falló (se intenta seguir): {e}")

    logger.info("Bootstrapping ValueData backend (training model, may take 30-40s)...")
    STATE.init()
    logger.info(
        f"Model ready. AUC={STATE.boot['metrics']['auc']:.3f}, "
        f"Brier={STATE.boot['metrics']['brier']:.4f}. "
        f"Today plan: {len(STATE.today_plan)} visits."
    )

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        STATE.tick, "interval",
        seconds=SCHEDULER_TICK_SEC, id="sim-tick",
        max_instances=1, coalesce=True,
    )
    # VIP deadline checker (interval 60s)
    from vip_deadline_cron import register_cron as register_vip_cron
    register_vip_cron(scheduler)

    scheduler.start()
    logger.info(f"Scheduler started: tick every {SCHEDULER_TICK_SEC}s")

    # Live SQL generator (inserta rows aleatorias en fpoc.simpli_visits)
    live_gen_start()

    # Simulador de comentarios alertables (off por default; se enciende por endpoint)
    comment_sim_start()

    # Driver simulation (Ronda 4): movimiento + entregas. Solo procesa fechas
    # con state=EN_CURSO (gateado por start_sim() vía day_state.transition).
    driver_sim_start()

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        live_gen_stop()
        comment_sim_stop()
        driver_sim_stop()


app = FastAPI(
    title="ValueData backend - Torre de Control",
    version="0.1.0",
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

# ============================================================================
# Routers
# ============================================================================
from seguimiento import router as seguimiento_router
from notifications import router as notifications_router
from preferences import router as preferences_router
from vip import router as vip_router
from priorities import router as priorities_router
from plan_diario import router as plan_diario_router
from watchlist import router as watchlist_router
from live_generator import (
    router as live_gen_router,
    start_scheduler as live_gen_start,
    stop_scheduler as live_gen_stop,
)
from mantenedores import router as mantenedores_router
from me import router as me_router
from comments import router as comments_router
from empresa_contactos import router as empresa_contactos_router
from motivo_classifier import router as motivo_classifier_router
from comment_simulator import (
    router as comment_sim_router,
    start_scheduler as comment_sim_start,
    stop_scheduler as comment_sim_stop,
)
from motivo_corrections import router as motivo_corrections_router
from drivers_whatsapp import router as drivers_whatsapp_router
from day_planning import router as day_planning_router
from day_state import router as day_state_router
from rutas import router as rutas_router
from seed_admin import router as seed_admin_router
from driver_sim import router as driver_sim_router, start_scheduler as driver_sim_start, stop_scheduler as driver_sim_stop
from search import router as search_router
from twilio_inbound import router as twilio_inbound_router, _legacy_router as twilio_legacy_router
from whatsapp_onboarding import router as whatsapp_onboarding_router
from agent_web import router as agent_web_router
# R7-F3: endpoints legacy extraídos de main.py (system/state/control/model/fleet)
from legacy_routes import (
    system_router,
    control_router,
    model_router,
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
app.include_router(live_gen_router)
app.include_router(mantenedores_router)
app.include_router(me_router)
app.include_router(comments_router)
app.include_router(empresa_contactos_router)
app.include_router(comment_sim_router)
app.include_router(motivo_classifier_router)
app.include_router(motivo_corrections_router)
app.include_router(drivers_whatsapp_router)
app.include_router(day_planning_router)
app.include_router(day_state_router)
app.include_router(rutas_router)
app.include_router(seed_admin_router)
app.include_router(driver_sim_router)
app.include_router(search_router)
app.include_router(twilio_inbound_router)
app.include_router(twilio_legacy_router)
app.include_router(whatsapp_onboarding_router)
app.include_router(agent_web_router)
app.include_router(system_router)
app.include_router(control_router)
app.include_router(model_router)
app.include_router(fleet_router)

"""dispatch_alert template wiring: real Content SIDs + 6 positional variables.

Regression guard for the production-blocking bug where `_template_for` returned
`HX..._STUB` placeholders (Twilio HTTP 400) and the dispatcher sent 3 named
variables instead of the templates' required 6 positional ones.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core import alert_dispatcher as disp
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.empresa import Empresa
from app.db.models.visita import Visita


@pytest_asyncio.fixture
async def sm() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _assert_six_string_vars(cvars: dict):
    assert set(cvars) == {"1", "2", "3", "4", "5", "6"}, cvars
    assert all(isinstance(v, str) and v != "" for v in cvars.values()), cvars


def test_template_for_uses_real_sids():
    for tipo in ("eta_breach", "eta_preview", "manual", "vip_deadline"):
        sid = disp._template_for(tipo)
        assert sid.startswith("HX") and "STUB" not in sid, f"{tipo} -> {sid}"
    # vip_deadline maps to a distinct template from the generic alert.
    assert disp._template_for("vip_deadline") != disp._template_for("eta_breach")


@pytest.mark.asyncio
async def test_build_message_eta_breach(sm):
    async with sm() as db:
        db.add(Empresa(empresa_id=1, nombre="Empresa Uno", activo=True))
        await db.flush()
        v = Visita(
            visita_id=1, dia_id=1, empresa_id=1, orden=1, cliente_nombre="Cli X",
            direccion="d", estado="no_entregado", motivo="SIN MORADORES",
            folio_cliente="FAL-1", es_vip=0,
        )
        db.add(v)
        await db.commit()
        alert = Alert(tipo="eta_breach", severity="alta", empresa_id=1, visita_id=1,
                      descripcion="atraso", estado="abierta")
        sid, cvars = await disp._build_template_message(db, alert)
    assert sid == disp.alerta_motivo_sid()
    _assert_six_string_vars(cvars)
    assert cvars["1"] == "ALTA"            # severity upper
    assert cvars["2"] == "SIN MORADORES"   # motivo
    assert cvars["3"] == "FAL-1"           # folio
    assert cvars["5"] == "Empresa Uno"     # empresa


@pytest.mark.asyncio
async def test_build_message_vip_deadline(sm):
    eta = datetime(2026, 6, 1, 10, 20, tzinfo=UTC)
    async with sm() as db:
        db.add(Empresa(empresa_id=1, nombre="Empresa Uno", activo=True))
        await db.flush()
        db.add(Visita(
            visita_id=2, dia_id=1, empresa_id=1, orden=1, cliente_nombre="Maria",
            direccion="d", estado="pendiente", es_vip=1, eta_estimada=eta,
        ))
        await db.commit()
        alert = Alert(tipo="vip_deadline", severity="critica", empresa_id=1, visita_id=2,
                      descripcion="vip", estado="abierta",
                      payload_json=json.dumps({"sim_now": (eta - timedelta(minutes=25)).isoformat()}))
        sid, cvars = await disp._build_template_message(db, alert)
    assert sid == disp.vip_deadline_sid()
    _assert_six_string_vars(cvars)
    assert cvars["1"] == "Maria"   # cliente
    assert cvars["2"] == "10:20"   # eta HH:MM
    assert cvars["3"] == "25"      # minutes remaining

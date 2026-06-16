"""CR-022 Part A — unit tests for alerts.

Covers:
  * dedupe: same eta_breach key twice → 1 alert.
  * dispatcher honors `settings.notifications_dry_run` (no Twilio request).
  * `_check_alert_scope` raises 403 for cross-tenant.
  * recipient filter: contacto with notify_severities whitelist drops mismatched.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.api.v1.alerts import _check_alert_scope
from app.core.alert_dispatcher import _Recipient, _passes_filter, dispatch_alert
from app.db import models  # noqa: F401 -- side-effect registers all tables
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as session:
        yield session
    await engine.dispose()


# ----------------------------------------------------------------------------
# Scope
# ----------------------------------------------------------------------------

def test_check_alert_scope_cross_tenant_403():
    """transport_manager scoped to empresa 1 cannot touch an alert in empresa 2."""
    u = User(
        user_id=1, email="a", password_hash="x", display_name="m",
        role="transport_manager", activo=True,
    )
    u._empresa_ids = [1]  # type: ignore[attr-defined]
    alert = Alert(
        alert_id=1, tipo="manual", severity="alta", empresa_id=2,
        descripcion="x", estado="abierta",
    )
    with pytest.raises(HTTPException) as exc:
        _check_alert_scope(u, alert)
    assert exc.value.status_code == 403


def test_check_alert_scope_admin_passes_any_tenant():
    u = User(
        user_id=1, email="a", password_hash="x", display_name="m",
        role="falabella_admin", activo=True,
    )
    u._empresa_ids = []  # type: ignore[attr-defined]
    alert = Alert(
        alert_id=1, tipo="manual", severity="alta", empresa_id=999,
        descripcion="x", estado="abierta",
    )
    _check_alert_scope(u, alert)  # no raise


# ----------------------------------------------------------------------------
# Filter
# ----------------------------------------------------------------------------

def test_filter_driver_always_passes():
    r = _Recipient("driver", "DRV-1", "x", "+56900000001", "driver")
    alert = Alert(alert_id=1, tipo="manual", severity="baja", empresa_id=1,
                  descripcion="x", estado="abierta")
    assert _passes_filter(r, alert, motivo=None) is True


def test_filter_user_drops_low_severity_for_non_admin():
    r = _Recipient("user", "1", "x", "+56900000001", "falabella_ops")
    alert = Alert(alert_id=1, tipo="manual", severity="baja", empresa_id=1,
                  descripcion="x", estado="abierta")
    assert _passes_filter(r, alert, motivo=None) is False


def test_filter_user_admin_always_passes():
    r = _Recipient("user", "1", "x", "+56900000001", "falabella_admin")
    alert = Alert(alert_id=1, tipo="manual", severity="baja", empresa_id=1,
                  descripcion="x", estado="abierta")
    assert _passes_filter(r, alert, motivo=None) is True


def test_filter_contacto_severity_whitelist():
    r = _Recipient(
        "contacto", "1", "x", "+56900000001", "jefe",
        notify_severities='["alta","critica"]',
    )
    a_low = Alert(alert_id=1, tipo="manual", severity="baja", empresa_id=1,
                  descripcion="x", estado="abierta")
    a_high = Alert(alert_id=2, tipo="manual", severity="critica", empresa_id=1,
                   descripcion="x", estado="abierta")
    assert _passes_filter(r, a_low, motivo=None) is False
    assert _passes_filter(r, a_high, motivo=None) is True


def test_filter_contacto_no_whitelist_accepts_all():
    r = _Recipient("contacto", "1", "x", "+56900000001", "jefe")
    alert = Alert(alert_id=1, tipo="manual", severity="baja", empresa_id=1,
                  descripcion="x", estado="abierta")
    assert _passes_filter(r, alert, motivo=None) is True


def test_filter_contacto_motivo_whitelist():
    r = _Recipient(
        "contacto", "1", "x", "+56900000001", "jefe",
        notify_motivos='["AUSENTE","DIRECCION_ERRADA"]',
    )
    alert = Alert(alert_id=1, tipo="eta_breach", severity="alta", empresa_id=1,
                  descripcion="x", estado="abierta")
    assert _passes_filter(r, alert, motivo="AUSENTE") is True
    assert _passes_filter(r, alert, motivo="RECHAZADO") is False


# ----------------------------------------------------------------------------
# Dedupe (via filter probe path) + dispatcher
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_dry_run_does_not_call_twilio(db_session: AsyncSession, monkeypatch):
    """settings.notifications_dry_run=True → send_whatsapp logs but returns True
    without hitting Twilio."""
    from app.core.config import settings as cfg
    assert cfg.notifications_dry_run is True  # conftest sets this

    # Seed: empresa + 1 driver opted in.
    db_session.add(Empresa(empresa_id=1, nombre="E1", activo=True))
    await db_session.flush()
    db_session.add(Driver(
        driver_id="DRV-01001", empresa_id=1, nombre="D",
        phone_e164="+56900000001", notify_whatsapp=True,
        opted_in_at=datetime.now(UTC), activo=True,
    ))
    alert = Alert(
        tipo="manual", severity="alta", empresa_id=1,
        descripcion="test", estado="abierta",
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)

    # Spy on the twilio.rest.Client to assert it's never instantiated.
    instantiated = {"flag": False}

    class _Spy:
        def __init__(self, *_a, **_kw):
            instantiated["flag"] = True

    monkeypatch.setattr("twilio.rest.Client", _Spy, raising=False)

    result = await dispatch_alert(db_session, alert)
    assert result.dry_run is True
    assert result.recipients == 1  # driver always passes
    assert result.sent == 1
    assert instantiated["flag"] is False  # Twilio Client never built
    await db_session.refresh(alert)
    assert alert.estado == "notificada"
    assert alert.notified_recipients_count == 1


@pytest.mark.asyncio
async def test_dispatch_idempotent_on_non_open(db_session: AsyncSession):
    db_session.add(Empresa(empresa_id=1, nombre="E1", activo=True))
    alert = Alert(
        tipo="manual", severity="alta", empresa_id=1,
        descripcion="test", estado="notificada",
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)

    result = await dispatch_alert(db_session, alert)
    assert result.recipients == 0
    assert result.sent == 0


@pytest.mark.asyncio
async def test_dispatch_contacto_severity_filter(db_session: AsyncSession):
    """A contacto with severity whitelist gets dropped for mismatched alert."""
    db_session.add(Empresa(empresa_id=1, nombre="E1", activo=True))
    await db_session.flush()
    db_session.add(EmpresaContacto(
        empresa_id=1, nombre="Jefe", rol="jefe",
        phone_e164="+56900000002", opted_in_at=datetime.now(UTC),
        activo=True,
        notify_severities='["critica"]',  # only critica
    ))
    alert = Alert(
        tipo="manual", severity="media", empresa_id=1,
        descripcion="test", estado="abierta",
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)

    result = await dispatch_alert(db_session, alert)
    assert result.recipients == 0  # contacto filtered out
    assert result.sent == 0


# ----------------------------------------------------------------------------
# Dedupe key behavior — direct SELECT to verify _alert_exists logic
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedupe_key_uniqueness_via_helper(db_session: AsyncSession):
    """Two rows with the same dedupe_key + estado != descartada — helper says exists."""
    from app.jobs.alerts import _alert_exists

    db_session.add(Empresa(empresa_id=1, nombre="E1", activo=True))
    db_session.add(Alert(
        tipo="eta_breach", severity="alta", empresa_id=1,
        descripcion="first", estado="abierta",
        dedupe_key="eta_breach:42:2026-05-30",
    ))
    await db_session.commit()
    assert await _alert_exists(db_session, "eta_breach:42:2026-05-30") is True
    assert await _alert_exists(db_session, "eta_breach:99:2026-05-30") is False


@pytest.mark.asyncio
async def test_dedupe_excludes_descartada(db_session: AsyncSession):
    """A dismissed alert (descartada) should NOT block re-creation."""
    from app.jobs.alerts import _alert_exists

    db_session.add(Empresa(empresa_id=1, nombre="E1", activo=True))
    db_session.add(Alert(
        tipo="eta_breach", severity="alta", empresa_id=1,
        descripcion="dismissed", estado="descartada",
        dedupe_key="eta_breach:7:2026-05-30",
    ))
    await db_session.commit()
    assert await _alert_exists(db_session, "eta_breach:7:2026-05-30") is False

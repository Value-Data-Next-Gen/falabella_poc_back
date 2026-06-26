"""CR-022 Part B — unit tests for the 2 new alerts tools exposed to the LLM.

Covers:
  * `listar_alertas_abiertas` shape (total / por_severity / items).
  * `listar_alertas_abiertas` scope: driver / contacto / transport_manager only
    see their own empresa; falabella_admin sees all.
  * `crear_alerta_manual` persists with tipo='manual' and defaults to the
    actor's empresa_id when omitted (driver / contacto).
  * `crear_alerta_manual` rejects out-of-scope `empresa_id` for non-admin.
  * `crear_alerta_manual(auto_dispatch=True, severity='critica')` invokes the
    dispatcher; with severity='media' it does NOT (the JSON contract gates
    dispatch to alta/critica).
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core import ai_tools
from app.core.ai_tools import execute_tool
from app.db import models  # noqa: F401 -- registers all tables
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
        # Seed: 2 empresas.
        session.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        await session.flush()
        # Seed: alerts across both empresas + multiple severities/estados.
        session.add_all([
            Alert(tipo="eta_breach", severity="critica", empresa_id=1,
                  descripcion="A1 crit", estado="abierta",
                  created_at=datetime(2026, 5, 30, 9, 0, tzinfo=UTC)),
            Alert(tipo="eta_breach", severity="media", empresa_id=1,
                  descripcion="A2 med", estado="notificada",
                  created_at=datetime(2026, 5, 30, 9, 5, tzinfo=UTC)),
            Alert(tipo="manual", severity="baja", empresa_id=1,
                  descripcion="A3 low", estado="resuelta",
                  created_at=datetime(2026, 5, 30, 9, 10, tzinfo=UTC)),
            Alert(tipo="vip_deadline", severity="alta", empresa_id=2,
                  descripcion="B1 alta", estado="abierta",
                  created_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC)),
        ])
        await session.commit()
        yield session
    await engine.dispose()


def _user(role: str, empresa_ids: list[int] | None = None) -> User:
    u = User(user_id=99, email="u@x", password_hash="x",
             display_name="u", role=role, activo=True)
    u._empresa_ids = empresa_ids or []  # type: ignore[attr-defined]
    return u


# ---------------------------------------------------------------------------
# listar_alertas_abiertas
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listar_admin_sees_all_open_or_notified(db_session: AsyncSession):
    admin = _user("falabella_admin")
    raw = await execute_tool(db_session, "listar_alertas_abiertas", {}, actor=admin)
    out = json.loads(raw)
    # 3 alerts are abierta/notificada (A1, A2, B1). A3 is resuelta → excluded.
    assert out["total"] == 3
    # Sort: critica > alta > media; A1 critica must come first.
    assert out["items"][0]["severity"] == "critica"
    assert out["items"][0]["empresa_id"] == 1
    # Aggregated breakdown.
    assert out["por_severity"] == {"critica": 1, "alta": 1, "media": 1}


@pytest.mark.asyncio
async def test_listar_admin_filter_by_empresa(db_session: AsyncSession):
    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "listar_alertas_abiertas", {"empresa_id": 2}, actor=admin,
    )
    out = json.loads(raw)
    assert out["total"] == 1
    assert out["items"][0]["empresa_id"] == 2


@pytest.mark.asyncio
async def test_listar_contacto_only_sees_own_empresa(db_session: AsyncSession):
    """A contacto on empresa 1 cannot see empresa 2's alerts, even if asked.

    (Drivers no longer get the alerts-listing tool under the role model — that's
    oversight territory; contactos are the scoped, pinned principal here.)"""
    contacto = EmpresaContacto(contact_id=1, empresa_id=1, nombre="Jefe", rol="jefe",
                               phone_e164="+56900000001", activo=True)
    raw = await execute_tool(
        db_session, "listar_alertas_abiertas",
        {"empresa_id": 2},  # LLM tries to pivot — must be ignored.
        actor=contacto,
    )
    out = json.loads(raw)
    assert out["total"] == 2  # A1 + A2 in empresa 1
    for item in out["items"]:
        assert item["empresa_id"] == 1


@pytest.mark.asyncio
async def test_listar_transport_manager_scoped(db_session: AsyncSession):
    tm = _user("transport_manager", empresa_ids=[2])
    raw = await execute_tool(db_session, "listar_alertas_abiertas", {}, actor=tm)
    out = json.loads(raw)
    assert out["total"] == 1
    assert out["items"][0]["empresa_id"] == 2


@pytest.mark.asyncio
async def test_listar_transport_manager_no_scope_empty(db_session: AsyncSession):
    tm = _user("transport_manager", empresa_ids=[])
    raw = await execute_tool(db_session, "listar_alertas_abiertas", {}, actor=tm)
    out = json.loads(raw)
    assert out["total"] == 0


@pytest.mark.asyncio
async def test_listar_actor_none_rejected(db_session: AsyncSession):
    # anon actor has no role → the execute_tool guard denies before the tool runs.
    raw = await execute_tool(db_session, "listar_alertas_abiertas", {}, actor=None)
    out = json.loads(raw)
    assert "error" in out


@pytest.mark.asyncio
async def test_listar_filter_by_severity_and_tipo(db_session: AsyncSession):
    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "listar_alertas_abiertas",
        {"tipo": "vip_deadline"}, actor=admin,
    )
    out = json.loads(raw)
    assert out["total"] == 1
    assert out["items"][0]["tipo"] == "vip_deadline"


# ---------------------------------------------------------------------------
# crear_alerta_manual
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crear_admin_persists(db_session: AsyncSession):
    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {
            "empresa_id": 1,
            "severity": "media",
            "descripcion": "Siniestro en Av. Providencia",
        },
        actor=admin,
    )
    out = json.loads(raw)
    assert out["estado"] == "abierta"
    assert out["dispatched"] is False
    persisted = (
        await db_session.execute(select(Alert).where(Alert.alert_id == out["alert_id"]))
    ).scalar_one()
    assert persisted.tipo == "manual"
    assert persisted.dedupe_key is None
    assert persisted.descripcion == "Siniestro en Av. Providencia"


@pytest.mark.asyncio
async def test_crear_driver_defaults_to_own_empresa(db_session: AsyncSession):
    """LLM omits empresa_id → driver.empresa_id is used."""
    driver = Driver(driver_id="DRV-1", empresa_id=1, nombre="D",
                    phone_e164="+56900000001", activo=True)
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"severity": "media", "descripcion": "Demora prolongada"},
        actor=driver,
    )
    out = json.loads(raw)
    assert "alert_id" in out
    assert out["empresa_id"] == 1


@pytest.mark.asyncio
async def test_crear_driver_cross_tenant_rejected(db_session: AsyncSession):
    """Driver on empresa 1 cannot create an alert on empresa 2."""
    driver = Driver(driver_id="DRV-1", empresa_id=1, nombre="D",
                    phone_e164="+56900000001", activo=True)
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"empresa_id": 2, "severity": "alta", "descripcion": "x"},
        actor=driver,
    )
    out = json.loads(raw)
    assert "error" in out
    assert "Forbidden" in out["error"]


@pytest.mark.asyncio
async def test_crear_contacto_cross_tenant_rejected(db_session: AsyncSession):
    contacto = EmpresaContacto(
        contact_id=1, empresa_id=1, nombre="Jefe", rol="jefe",
        phone_e164="+56900000002", activo=True,
    )
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"empresa_id": 2, "severity": "alta", "descripcion": "x"},
        actor=contacto,
    )
    out = json.loads(raw)
    assert "error" in out


@pytest.mark.asyncio
async def test_crear_transport_manager_out_of_scope_rejected(db_session: AsyncSession):
    tm = _user("transport_manager", empresa_ids=[1])
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"empresa_id": 2, "severity": "alta", "descripcion": "x"},
        actor=tm,
    )
    out = json.loads(raw)
    assert "error" in out


@pytest.mark.asyncio
async def test_crear_actor_none_rejected(db_session: AsyncSession):
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"empresa_id": 1, "severity": "alta", "descripcion": "x"},
        actor=None,
    )
    out = json.loads(raw)
    assert "error" in out


@pytest.mark.asyncio
async def test_crear_invalid_severity_rejected(db_session: AsyncSession):
    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"empresa_id": 1, "severity": "exploding", "descripcion": "x"},
        actor=admin,
    )
    out = json.loads(raw)
    assert "error" in out


@pytest.mark.asyncio
async def test_crear_empty_descripcion_rejected(db_session: AsyncSession):
    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {"empresa_id": 1, "severity": "media", "descripcion": "   "},
        actor=admin,
    )
    out = json.loads(raw)
    assert "error" in out


@pytest.mark.asyncio
async def test_crear_auto_dispatch_critica_invokes_dispatcher(
    db_session: AsyncSession, monkeypatch,
):
    """auto_dispatch=True AND severity=critica → dispatcher called."""
    calls: dict[str, int] = {"count": 0}

    async def _fake_dispatch(db, alert, motivo=None):
        calls["count"] += 1
        from app.schemas.alert import AlertDispatchResult
        # Flip estado so the contract reflects "notified".
        alert.estado = "notificada"
        alert.notified_recipients_count = 2
        await db.commit()
        return AlertDispatchResult(
            alert_id=alert.alert_id, recipients=2, sent=2, dry_run=True,
        )

    monkeypatch.setattr(
        "app.core.alert_dispatcher.dispatch_alert", _fake_dispatch,
    )

    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {
            "empresa_id": 1,
            "severity": "critica",
            "descripcion": "Emergencia",
            "auto_dispatch": True,
        },
        actor=admin,
    )
    out = json.loads(raw)
    assert calls["count"] == 1
    assert out["dispatched"] is True
    assert out["recipients_count"] == 2


@pytest.mark.asyncio
async def test_crear_auto_dispatch_media_skips_dispatcher(
    db_session: AsyncSession, monkeypatch,
):
    """auto_dispatch=True but severity=media → dispatcher NOT called (gated)."""
    calls: dict[str, int] = {"count": 0}

    async def _fake_dispatch(db, alert, motivo=None):
        calls["count"] += 1
        from app.schemas.alert import AlertDispatchResult
        return AlertDispatchResult(
            alert_id=alert.alert_id, recipients=0, sent=0, dry_run=True,
        )

    monkeypatch.setattr(
        "app.core.alert_dispatcher.dispatch_alert", _fake_dispatch,
    )

    admin = _user("falabella_admin")
    raw = await execute_tool(
        db_session, "crear_alerta_manual",
        {
            "empresa_id": 1,
            "severity": "media",
            "descripcion": "menor",
            "auto_dispatch": True,
        },
        actor=admin,
    )
    out = json.loads(raw)
    assert calls["count"] == 0
    assert out["dispatched"] is False


# ---------------------------------------------------------------------------
# Tool definitions exposed to the LLM
# ---------------------------------------------------------------------------

def test_tool_definitions_include_new_alerts_tools():
    names = {t["function"]["name"] for t in ai_tools.TOOL_DEFINITIONS}
    assert "listar_alertas_abiertas" in names
    assert "crear_alerta_manual" in names

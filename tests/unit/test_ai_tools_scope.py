"""Tenant-scope enforcement for the bot's INFO tools.

Regression guard: contar_entidades / listar_conductores / resumen_empresa /
verificar_compliance_documentos used to take empresa_id straight from the LLM
with no actor check, so a driver/contacto could read another empresa (the
"40 conductores" cross-tenant leak). Now they floor to the actor's empresa.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core.ai_tools import execute_tool
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool, echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        s.add_all([Empresa(empresa_id=1, nombre="E1", activo=True),
                   Empresa(empresa_id=2, nombre="E2", activo=True)])
        await s.flush()
        # empresa 1: 2 drivers; empresa 2: 3 drivers
        s.add_all([
            Driver(driver_id="D1A", empresa_id=1, nombre="d1a", activo=True),
            Driver(driver_id="D1B", empresa_id=1, nombre="d1b", activo=True),
            Driver(driver_id="D2A", empresa_id=2, nombre="d2a", activo=True),
            Driver(driver_id="D2B", empresa_id=2, nombre="d2b", activo=True),
            Driver(driver_id="D2C", empresa_id=2, nombre="d2c", activo=True),
        ])
        await s.commit()
        yield s


def _contacto1() -> EmpresaContacto:
    """A scoped oversight principal bound to empresa 1. Used to exercise the
    info tools' tenant-scope flooring under the role model (drivers no longer
    get these oversight tools; contactos do, and are pinned to one empresa)."""
    return EmpresaContacto(contact_id=1, empresa_id=1, nombre="c1", rol="jefe", activo=True)


def _admin() -> User:
    u = User(user_id=99, email="a@td.cl", password_hash="x", display_name="A",
             role="falabella_admin", activo=True)
    u._empresa_ids = []  # type: ignore[attr-defined]
    return u


@pytest.mark.asyncio
async def test_contar_floored_to_actor_empresa(db_session):
    # driver of empresa 1 → only empresa 1's 2 drivers, regardless of request.
    r = json.loads(await execute_tool(db_session, "contar_entidades",
                                      {"entidad": "conductores"}, actor=_contacto1()))
    assert r["total"] == 2 and r["empresa_id"] == 1, r
    # even asking for empresa 2 is overridden to the driver's own empresa.
    r2 = json.loads(await execute_tool(db_session, "contar_entidades",
                                       {"entidad": "conductores", "empresa_id": 2}, actor=_contacto1()))
    assert r2["total"] == 2 and r2["empresa_id"] == 1, r2


@pytest.mark.asyncio
async def test_admin_sees_all(db_session):
    r = json.loads(await execute_tool(db_session, "contar_entidades",
                                      {"entidad": "conductores"}, actor=_admin()))
    assert r["total"] == 5, r  # both empresas


@pytest.mark.asyncio
async def test_listar_conductores_scoped(db_session):
    r = json.loads(await execute_tool(db_session, "listar_conductores", {}, actor=_contacto1()))
    assert {c["empresa_id"] for c in r["conductores"]} == {1}, r


@pytest.mark.asyncio
async def test_resumen_empresa_cross_tenant_denied(db_session):
    deny = json.loads(await execute_tool(db_session, "resumen_empresa",
                                         {"empresa_id": 2}, actor=_contacto1()))
    assert "error" in deny, deny
    ok = json.loads(await execute_tool(db_session, "resumen_empresa",
                                       {"empresa_id": 1}, actor=_contacto1()))
    assert ok.get("empresa") == "E1", ok


@pytest.mark.asyncio
async def test_compliance_cross_tenant_denied(db_session):
    # driver of empresa 1 cannot inspect a conductor of empresa 2.
    r = json.loads(await execute_tool(db_session, "verificar_compliance_documentos",
                                      {"tipo_entidad": "conductor", "entidad_id": "D2A"},
                                      actor=_contacto1()))
    assert "error" in r, r


@pytest.mark.asyncio
async def test_no_actor_denied(db_session):
    r = json.loads(await execute_tool(db_session, "contar_entidades",
                                      {"entidad": "conductores"}, actor=None))
    assert "error" in r, r

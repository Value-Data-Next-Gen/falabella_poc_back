"""obtener_reporte bot tool: role-scoped operational report."""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

from app.core.ai_tools import execute_tool
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User
from app.db.models.visita import Visita


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        s.add_all([Empresa(empresa_id=1, nombre="E1", activo=True),
                   Empresa(empresa_id=2, nombre="E2", activo=True)])
        await s.flush()
        d = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 10), estado="CERRADO")
        s.add(d)
        await s.flush()
        # 3 entregado + 1 no_entregado → success 75%
        for est, mot in [("entregado", None), ("entregado", None), ("entregado", None),
                         ("no_entregado", "SIN MORADORES")]:
            s.add(Visita(dia_id=d.dia_id, empresa_id=1, orden=1, cliente_nombre="c",
                         direccion="d", estado=est, motivo=mot, region="RM"))
        await s.commit()
        yield s
    await engine.dispose()


def _contacto(eid: int) -> EmpresaContacto:
    return EmpresaContacto(contact_id=1, empresa_id=eid, nombre="Jefe", rol="jefe", activo=True)


@pytest.mark.asyncio
async def test_reporte_for_own_empresa(db_session):
    out = json.loads(await execute_tool(db_session, "obtener_reporte", {}, actor=_contacto(1)))
    assert out["empresa_id"] == 1
    assert out["visitas"] == 4
    assert out["entregado"] == 3
    assert out["success_pct"] == 75.0
    assert out["top_motivos"][0]["motivo"] == "SIN MORADORES"


@pytest.mark.asyncio
async def test_reporte_scoped_to_actor_empresa(db_session):
    # contacto of empresa 1 asking for empresa 2 is floored back to empresa 1,
    # and the result carries an `aviso` so the bot phrases it accurately.
    out = json.loads(await execute_tool(
        db_session, "obtener_reporte", {"empresa_id": 2}, actor=_contacto(1)))
    assert out["empresa_id"] == 1
    assert "aviso" in out and "tu empresa" in out["aviso"].lower()


@pytest.mark.asyncio
async def test_reporte_admin_must_specify_empresa(db_session):
    admin = User(user_id=1, role="falabella_admin", email="a@td.cl", display_name="A", activo=True)
    admin._empresa_ids = []  # type: ignore[attr-defined]
    out = json.loads(await execute_tool(db_session, "obtener_reporte", {}, actor=admin))
    assert "error" in out  # admin has no implicit single empresa


@pytest.mark.asyncio
async def test_reporte_not_available_to_driver(db_session):
    out = json.loads(await execute_tool(
        db_session, "obtener_reporte", {}, actor=Driver(driver_id="D1", empresa_id=1, nombre="x")))
    assert "no disponible" in out["error"].lower()

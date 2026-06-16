"""CR-024 Part A3 — unit tests for the 2 new ai_tools (cliente).

Covers:
  * obtener_info_cliente_por_folio returns the cliente snapshot (vip + notas).
  * Driver actor: out-of-empresa folio → "Folio no encontrado en sus rutas".
  * cancelar_visita_manual updates the visita state with proper motivo.
  * Driver cannot cancel a visita in a different empresa.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

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

from app.core.ai_tools import execute_tool
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.visita import Visita


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
        # 2 empresas + 1 cliente VIP linked to empresa 1 only.
        session.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        await session.flush()
        day = DiaOperativo(empresa_id=1, fecha=__import__("datetime").date(2026, 6, 1), estado="EN_CURSO")
        session.add(day)
        await session.flush()
        cli = Cliente(
            nombre="Juana Perez", telefono="+56911112222",
            rut="RUT-VIP", es_vip=True, vip_razon="frecuente",
            notas_operativas="Llamar antes",
            direccion_default="Apoquindo 100", comuna_default="Las Condes",
            geocoding_status="pending", geocoding_attempts=0,
        )
        session.add(cli)
        await session.flush()
        # Visita with folio "FOL-1" in empresa 1.
        v1 = Visita(
            dia_id=day.dia_id, empresa_id=1, orden=1,
            cliente_id=cli.cliente_id, cliente_nombre="Juana Perez",
            direccion="X", estado="pendiente", folio_cliente="FOL-1",
        )
        session.add(v1)
        # CR-027: link cliente <-> empresa derived from the visita above.
        # Driver of empresa 1, driver of empresa 2.
        session.add_all([
            Driver(driver_id="DRV-01001", empresa_id=1, nombre="D1", activo=True),
            Driver(driver_id="DRV-02001", empresa_id=2, nombre="D2", activo=True),
        ])
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_obtener_info_cliente_por_folio_admin_sees_vip(db_session):
    admin = User(user_id=10, email="a@td.cl", password_hash="x",
                 display_name="A", role="falabella_admin", activo=True)
    admin._empresa_ids = []  # type: ignore[attr-defined]
    result = await execute_tool(
        db_session, "obtener_info_cliente_por_folio",
        {"folio_cliente": "FOL-1"}, actor=admin,
    )
    data = json.loads(result)
    assert data["nombre"] == "Juana Perez"
    assert data["es_vip"] is True
    assert data["vip_razon"] == "frecuente"
    assert data["notas_operativas"] == "Llamar antes"
    assert data["telefono"] == "+56911112222"


@pytest.mark.asyncio
async def test_obtener_info_driver_other_empresa_not_found(db_session):
    """Driver de empresa 2 NO ve el folio (que está en empresa 1)."""
    drv2 = (await db_session.execute(
        select(Driver).where(Driver.driver_id == "DRV-02001")
    )).scalar_one()
    result = await execute_tool(
        db_session, "obtener_info_cliente_por_folio",
        {"folio_cliente": "FOL-1"}, actor=drv2,
    )
    data = json.loads(result)
    assert "error" in data
    assert "no encontrado" in data["error"].lower()


@pytest.mark.asyncio
async def test_obtener_info_unknown_folio(db_session):
    admin = User(user_id=10, email="a@td.cl", password_hash="x",
                 display_name="A", role="falabella_admin", activo=True)
    admin._empresa_ids = []  # type: ignore[attr-defined]
    result = await execute_tool(
        db_session, "obtener_info_cliente_por_folio",
        {"folio_cliente": "ZZZ"}, actor=admin,
    )
    data = json.loads(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_cancelar_visita_manual_admin_ok(db_session):
    admin = User(user_id=10, email="a@td.cl", password_hash="x",
                 display_name="A", role="falabella_admin", activo=True)
    admin._empresa_ids = []  # type: ignore[attr-defined]
    v1 = (await db_session.execute(
        select(Visita).where(Visita.folio_cliente == "FOL-1")
    )).scalar_one()
    result = await execute_tool(
        db_session, "cancelar_visita_manual",
        {"visita_id": v1.visita_id, "motivo": "Cliente confirmó cancelación"},
        actor=admin,
    )
    data = json.loads(result)
    assert data.get("ok") is True
    assert data["visita_id"] == v1.visita_id
    await db_session.refresh(v1)
    assert v1.estado == "cancelado"
    assert v1.motivo and v1.motivo.startswith("Cancelado: ")


@pytest.mark.asyncio
async def test_cancelar_visita_other_empresa_forbidden(db_session):
    """Driver empresa 2 intenta cancelar visita de empresa 1 → error."""
    drv2 = (await db_session.execute(
        select(Driver).where(Driver.driver_id == "DRV-02001")
    )).scalar_one()
    v1 = (await db_session.execute(
        select(Visita).where(Visita.folio_cliente == "FOL-1")
    )).scalar_one()
    result = await execute_tool(
        db_session, "cancelar_visita_manual",
        {"visita_id": v1.visita_id, "motivo": "no quiero"},
        actor=drv2,
    )
    data = json.loads(result)
    assert "error" in data
    assert "Forbidden" in data["error"]


@pytest.mark.asyncio
async def test_cancelar_visita_already_terminal(db_session):
    """No se puede cancelar una visita ya entregada."""
    v1 = (await db_session.execute(
        select(Visita).where(Visita.folio_cliente == "FOL-1")
    )).scalar_one()
    v1.estado = "entregado"
    await db_session.commit()
    admin = User(user_id=10, email="a@td.cl", password_hash="x",
                 display_name="A", role="falabella_admin", activo=True)
    admin._empresa_ids = []  # type: ignore[attr-defined]
    result = await execute_tool(
        db_session, "cancelar_visita_manual",
        {"visita_id": v1.visita_id, "motivo": "tarde"},
        actor=admin,
    )
    data = json.loads(result)
    assert "error" in data
    assert data.get("estado") == "entregado"

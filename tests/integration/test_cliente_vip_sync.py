"""CR-024 Part A1 — PATCH cliente propagates VIP changes to active visitas.

Coverage:
  * PATCH `es_vip=true` updates `visitas.es_vip=1` only for visitas in
    pendiente/en_camino AND in BORRADOR/VALIDADO/EN_CURSO days.
  * Visitas in `entregado` / `cancelado` / `no_entregado` stay untouched.
  * Visitas in CERRADO days stay untouched.
  * PATCH that doesn't change `es_vip` (e.g. `notas_operativas`-only) doesn't
    run the bulk UPDATE, doesn't add `sync_visitas_count` to the response.
  * Response includes `sync_visitas_count` when sync ran.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date

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

from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(_engine: async_sessionmaker[AsyncSession]) -> dict:
    """Seed: cliente served by empresa 1, with visitas in different states/days.

    - v_pend_encurso : pendiente, EN_CURSO day → MUST update.
    - v_camino_validado : en_camino, VALIDADO day → MUST update.
    - v_pend_borrador : pendiente, BORRADOR day → MUST update.
    - v_pend_cerrado : pendiente, CERRADO day → MUST NOT update (closed day).
    - v_entregado : entregado, EN_CURSO day → MUST NOT update (final state).
    """
    async with _engine() as db:
        db.add_all([Empresa(empresa_id=1, nombre="E1", activo=True)])
        await db.flush()
        days = [
            DiaOperativo(dia_id=1, empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO"),
            DiaOperativo(dia_id=2, empresa_id=1, fecha=date(2026, 6, 2), estado="VALIDADO"),
            DiaOperativo(dia_id=3, empresa_id=1, fecha=date(2026, 6, 3), estado="BORRADOR"),
            DiaOperativo(dia_id=4, empresa_id=1, fecha=date(2026, 5, 30), estado="CERRADO"),
        ]
        db.add_all(days)
        await db.flush()

        cli = Cliente(
            nombre="VIP-Candidate", rut="RUT-1",
            es_vip=False, geocoding_status="pending", geocoding_attempts=0,
        )
        db.add(cli)
        await db.flush()

        # Visitas across states / days.
        visitas = [
            Visita(visita_id=101, ruta_id=None, dia_id=1, empresa_id=1, orden=1,
                   cliente_id=cli.cliente_id, cliente_nombre="VIP-Candidate",
                   direccion="A", estado="pendiente", es_vip=0),
            Visita(visita_id=102, ruta_id=None, dia_id=2, empresa_id=1, orden=2,
                   cliente_id=cli.cliente_id, cliente_nombre="VIP-Candidate",
                   direccion="B", estado="en_camino", es_vip=0),
            Visita(visita_id=103, ruta_id=None, dia_id=3, empresa_id=1, orden=3,
                   cliente_id=cli.cliente_id, cliente_nombre="VIP-Candidate",
                   direccion="C", estado="pendiente", es_vip=0),
            Visita(visita_id=104, ruta_id=None, dia_id=4, empresa_id=1, orden=4,
                   cliente_id=cli.cliente_id, cliente_nombre="VIP-Candidate",
                   direccion="D", estado="pendiente", es_vip=0),
            Visita(visita_id=105, ruta_id=None, dia_id=1, empresa_id=1, orden=5,
                   cliente_id=cli.cliente_id, cliente_nombre="VIP-Candidate",
                   direccion="E", estado="entregado", es_vip=0),
        ]
        db.add_all(visitas)
        # CR-027: cliente <-> empresa link is implicit via the visitas above.
        await db.commit()
        return {"engine_sm": _engine, "cliente_id": cli.cliente_id}


def _override_admin():
    async def _stub() -> User:
        u = User(user_id=10, email="adm@td.cl", password_hash="x",
                 display_name="Adm", role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(seeded: dict):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    def make() -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _override_admin()
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def test_patch_vip_true_propagates_to_active_visitas(client_factory, seeded):
    """es_vip true → 3 visitas pendientes/en_camino en days no-CERRADO se actualizan."""
    c = client_factory()
    r = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}", json={"es_vip": True}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["es_vip"] is True
    # 3 active visitas in non-CERRADO days (ids 101, 102, 103). v104 is in
    # CERRADO; v105 is entregado.
    assert body["sync_visitas_count"] == 3


def test_patch_vip_false_propagates_too(client_factory, seeded):
    """Turning VIP off also propagates (toggle invariant)."""
    sm = seeded["engine_sm"]
    # First make the cliente VIP=true and visitas=1 in memory.
    c = client_factory()
    c.patch(f"/api/v1/clientes/{seeded['cliente_id']}", json={"es_vip": True})

    r = c.patch(f"/api/v1/clientes/{seeded['cliente_id']}", json={"es_vip": False})
    assert r.status_code == 200
    body = r.json()
    assert body["es_vip"] is False
    assert body["sync_visitas_count"] == 3


def test_patch_notas_no_sync(client_factory, seeded):
    """Cambiar solo notas_operativas no debe correr el bulk UPDATE ni traer
    sync_visitas_count en la respuesta (omitido cuando es None)."""
    c = client_factory()
    r = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}",
        json={"notas_operativas": "Entregar entre 9 y 11"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notas_operativas"] == "Entregar entre 9 y 11"
    # Por la config del schema (None default), debe estar ausente o null.
    assert body.get("sync_visitas_count") in (None,)


def test_patch_same_vip_value_no_sync(client_factory, seeded):
    """Si es_vip se manda igual al valor actual → no se gatilla sync."""
    c = client_factory()
    r = c.patch(
        f"/api/v1/clientes/{seeded['cliente_id']}", json={"es_vip": False}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("sync_visitas_count") in (None,)


@pytest.mark.asyncio
async def test_visitas_in_cerrado_day_not_touched(seeded):
    """Verificamos en la DB que la visita 104 (CERRADO) NO cambió de es_vip."""
    sm = seeded["engine_sm"]
    # PATCH directly via the override pattern.
    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_admin()
    try:
        with TestClient(app) as c:
            r = c.patch(
                f"/api/v1/clientes/{seeded['cliente_id']}", json={"es_vip": True}
            )
            assert r.status_code == 200
        async with sm() as db:
            v104 = (await db.execute(select(Visita).where(Visita.visita_id == 104))).scalar_one()
            v105 = (await db.execute(select(Visita).where(Visita.visita_id == 105))).scalar_one()
            v101 = (await db.execute(select(Visita).where(Visita.visita_id == 101))).scalar_one()
            # v101 active EN_CURSO → updated
            assert int(v101.es_vip or 0) == 1
            # v104 in CERRADO day → unchanged
            assert int(v104.es_vip or 0) == 0
            # v105 entregado → unchanged
            assert int(v105.es_vip or 0) == 0
    finally:
        app.dependency_overrides.clear()

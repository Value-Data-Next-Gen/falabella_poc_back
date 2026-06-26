"""CR-028 Part A — POST /api/v1/operacion/visitas/{id}/cancel.

Verifies:
  * Happy path: estado → cancelado, motivo + comentario persisted, audit row.
  * Unknown motivo_codigo → 400.
  * Inactive motivo → 400.
  * Visita in terminal estado (entregado / no_entregado / cancelado) → 409.
  * Dia CERRADO → 400.
  * Default comentario applied when omitted.
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
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.motivo import Motivo
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento
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
async def seeded(_engine):
    async with _engine() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        d = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        d_cerrado = DiaOperativo(empresa_id=1, fecha=date(2026, 5, 30), estado="CERRADO")
        db.add_all([d, d_cerrado])
        await db.flush()
        # 3 motivos: 2 activos, 1 inactivo.
        m_ok = Motivo(codigo="SIN MORADORES", descripcion="d", activo=True)
        m_other = Motivo(codigo="CLIENTE RECHAZA", descripcion="d", activo=True)
        m_inactive = Motivo(codigo="OBSOLETO", descripcion="d", activo=False)
        db.add_all([m_ok, m_other, m_inactive])
        await db.flush()
        v_pending = Visita(
            dia_id=d.dia_id, empresa_id=1, orden=1,
            cliente_nombre="C", direccion="addr", estado="pendiente",
        )
        v_entregado = Visita(
            dia_id=d.dia_id, empresa_id=1, orden=2,
            cliente_nombre="C2", direccion="addr2", estado="entregado",
        )
        v_in_closed = Visita(
            dia_id=d_cerrado.dia_id, empresa_id=1, orden=1,
            cliente_nombre="C3", direccion="addr3", estado="pendiente",
        )
        db.add_all([v_pending, v_entregado, v_in_closed])
        await db.commit()
        return {
            "engine_sm": _engine,
            "v_pending_id": v_pending.visita_id,
            "v_entregado_id": v_entregado.visita_id,
            "v_in_closed_id": v_in_closed.visita_id,
        }


def _override_admin():
    async def _stub() -> User:
        u = User(user_id=10, email="adm@td.cl", password_hash="x",
                 display_name="A", role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client(seeded):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _override_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


async def test_cancel_happy_path(client, seeded):
    r = client.post(
        f"/api/v1/operacion/visitas/{seeded['v_pending_id']}/cancel",
        json={"motivo_codigo": "SIN MORADORES", "comentario": "no atienden"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["estado"] == "cancelado"
    assert body["motivo"] == "SIN MORADORES"
    assert body["motivo_comentario"] == "no atienden"
    # Audit row present.
    async with seeded["engine_sm"]() as db:
        rows = (
            await db.execute(
                select(VisitaEvento).where(VisitaEvento.visita_id == seeded["v_pending_id"])
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].tipo == "cancelada"


async def test_cancel_unknown_motivo_400(client, seeded):
    r = client.post(
        f"/api/v1/operacion/visitas/{seeded['v_pending_id']}/cancel",
        json={"motivo_codigo": "INVENTADO_INEXISTENTE"},
    )
    assert r.status_code == 400, r.text
    assert "no existe" in r.json()["detail"].lower()


async def test_cancel_inactive_motivo_400(client, seeded):
    r = client.post(
        f"/api/v1/operacion/visitas/{seeded['v_pending_id']}/cancel",
        json={"motivo_codigo": "OBSOLETO"},
    )
    assert r.status_code == 400, r.text


async def test_cancel_terminal_estado_409(client, seeded):
    r = client.post(
        f"/api/v1/operacion/visitas/{seeded['v_entregado_id']}/cancel",
        json={"motivo_codigo": "SIN MORADORES"},
    )
    assert r.status_code == 409, r.text


async def test_cancel_dia_cerrado_400(client, seeded):
    r = client.post(
        f"/api/v1/operacion/visitas/{seeded['v_in_closed_id']}/cancel",
        json={"motivo_codigo": "SIN MORADORES"},
    )
    assert r.status_code == 400, r.text


async def test_cancel_default_comentario(client, seeded):
    r = client.post(
        f"/api/v1/operacion/visitas/{seeded['v_pending_id']}/cancel",
        json={"motivo_codigo": "CLIENTE RECHAZA"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["motivo_comentario"] == "Cancelado por torre"

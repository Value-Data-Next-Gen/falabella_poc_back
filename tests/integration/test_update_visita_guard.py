"""PATCH /visitas/{id}: blocked on a CERRADO día, and estado is validated."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
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
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def seeded() -> AsyncIterator[dict]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()
        d_open = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        d_closed = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 2), estado="CERRADO")
        db.add_all([d_open, d_closed])
        await db.flush()
        v_open = Visita(dia_id=d_open.dia_id, empresa_id=1, orden=1, cliente_nombre="c", direccion="d", estado="pendiente")
        v_closed = Visita(dia_id=d_closed.dia_id, empresa_id=1, orden=1, cliente_nombre="c", direccion="d", estado="entregado")
        db.add_all([v_open, v_closed])
        await db.commit()
        yield {"sm": sm, "v_open": v_open.visita_id, "v_closed": v_closed.visita_id}
    await engine.dispose()


@pytest.fixture
def client(seeded):
    sm = seeded["sm"]

    async def _db():
        async with sm() as s:
            yield s

    async def _admin() -> User:
        u = User(user_id=1, email="a@td.cl", password_hash="x", display_name="A",
                 role="falabella_admin", activo=True)
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_patch_blocked_on_cerrado_dia(client, seeded):
    r = client.patch(f"/api/v1/operacion/visitas/{seeded['v_closed']}", json={"notas": "x"})
    assert r.status_code == 400, r.text


def test_patch_invalid_estado_rejected(client, seeded):
    r = client.patch(f"/api/v1/operacion/visitas/{seeded['v_open']}", json={"estado": "garbage"})
    assert r.status_code == 400, r.text


def test_patch_valid_estado_ok(client, seeded):
    r = client.patch(f"/api/v1/operacion/visitas/{seeded['v_open']}", json={"estado": "entregado"})
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "entregado"

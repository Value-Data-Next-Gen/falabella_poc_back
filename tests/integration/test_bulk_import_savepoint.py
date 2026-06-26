"""Bulk-import partial-failure handling (per-row SAVEPOINT).

Regression test for the batch-nuking bug: the importers used to call a bare
`db.rollback()` on the first IntegrityError, which rolled back the ENTIRE
transaction — discarding every previously-flushed row while still reporting
them as "creado". A single bad row therefore silently lost all the good rows.

With per-row savepoints, a bad row rolls back only itself; the valid rows are
committed. We trigger the collision on `phone_e164` (unique at the model level;
`rut`'s uniqueness only exists in a migration, which tests don't run).

Persistence is verified purely over HTTP (a second colliding import only fails
if the first row was actually committed) to avoid cross-event-loop access to the
in-memory engine.
"""
from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator

import pytest_asyncio
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest_asyncio.fixture(scope="function")
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sessionmaker
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def seeded(_engine: async_sessionmaker[AsyncSession]) -> dict:
    async with _engine() as db:
        db.add(Empresa(empresa_id=1, nombre="Empresa Uno", activo=True))
        await db.commit()
    return {"engine_sm": _engine}


@pytest.fixture
def admin_client(seeded: dict):
    sessionmaker = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    async def _admin() -> User:
        u = User(
            user_id=10, email="admin@td.cl", password_hash="x",
            display_name="Admin", role="falabella_admin", activo=True,
        )
        u._empresa_ids = []  # type: ignore[attr-defined]
        return u

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _conductores_xlsx(data_rows: list[list[str]]) -> bytes:
    """Build a conductores upload: row1=headers, row2=descriptions, row3+=data."""
    wb = Workbook()
    ws = wb.active
    ws.append(["nombre", "telefono"])          # headers
    ws.append(["Nombre completo", "E.164"])    # descriptions (skipped by parser)
    for row in data_rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _import(client: TestClient, data_rows: list[list[str]]) -> dict:
    r = client.post(
        "/api/v1/empresas/1/conductores/cargar-excel",
        files={"file": ("conductores.xlsx", _conductores_xlsx(data_rows), _XLSX_MIME)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_one_bad_row_does_not_discard_the_good_rows(admin_client):
    # Two rows share a phone_e164 (unique) → the 2nd row must fail, the 1st must
    # survive. Pre-fix, the bare rollback would have wiped Alice too.
    body = _import(admin_client, [
        ["Alice", "+56900000001"],
        ["Bob", "+56900000001"],  # duplicate phone → IntegrityError
    ])
    assert body["creados"] == 1, body
    assert body["fallidos"] == 1, body

    # Persistence check over HTTP: importing Alice's phone again must now fail,
    # which only happens if Alice was actually committed by the first import.
    body2 = _import(admin_client, [["Charlie", "+56900000001"]])
    assert body2["creados"] == 0, body2
    assert body2["fallidos"] == 1, body2


def test_unique_driver_ids_within_one_upload(admin_client):
    # If id generation collided, one insert would fail on the PK → creados < 3.
    body = _import(admin_client, [
        ["Ana", "+56900000010"],
        ["Beto", "+56900000011"],
        ["Caro", "+56900000012"],
    ])
    assert body["creados"] == 3, body
    assert body["fallidos"] == 0, body

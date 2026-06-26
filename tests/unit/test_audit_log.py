"""CR-028 Part A — unit tests for `app.core.audit.log_visita_evento`.

Covered:
  * happy path: creates a row with serialized payload.
  * unknown `tipo` → ValueError before any DB write.
  * None payload → payload_json stored as NULL.
  * Oversized payload → truncated and marked.
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

os.environ.setdefault("DB_TEST_URL", "sqlite+aiosqlite:///:memory:")

from app.core.audit import log_visita_evento
from app.db import models  # noqa: F401  ensure metadata registered
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
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
        # Seed a visita so the FK is satisfied.
        e = Empresa(empresa_id=1, nombre="E1", activo=True)
        d = DiaOperativo(empresa_id=1, fecha=__import__("datetime").date(2026, 6, 1), estado="BORRADOR")
        session.add_all([e, d])
        await session.flush()
        v = Visita(
            dia_id=d.dia_id, empresa_id=1, orden=1,
            cliente_nombre="C1", direccion="addr", estado="pendiente",
        )
        session.add(v)
        await session.commit()
        # stash on the session for the test to grab.
        session.info["visita_id"] = v.visita_id
        yield session
    await engine.dispose()


async def test_log_visita_evento_creates_row(db: AsyncSession) -> None:
    visita_id = db.info["visita_id"]
    await log_visita_evento(
        db,
        visita_id=visita_id,
        tipo="orden_change",
        user_id=42,
        payload={"old_orden": 1, "nuevo_orden": 5},
    )
    await db.commit()
    rows = (await db.execute(select(VisitaEvento))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.visita_id == visita_id
    assert row.tipo == "orden_change"
    assert row.user_id == 42
    parsed = json.loads(row.payload_json)
    assert parsed == {"old_orden": 1, "nuevo_orden": 5}


async def test_log_visita_evento_rejects_unknown_tipo(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="Unknown visita_eventos.tipo"):
        await log_visita_evento(
            db,
            visita_id=db.info["visita_id"],
            tipo="not_a_real_tipo",
            user_id=1,
            payload=None,
        )
    # Nothing was committed.
    rows = (await db.execute(select(VisitaEvento))).scalars().all()
    assert rows == []


async def test_log_visita_evento_null_payload(db: AsyncSession) -> None:
    await log_visita_evento(
        db,
        visita_id=db.info["visita_id"],
        tipo="estado_change",
        user_id=None,
        payload=None,
    )
    await db.commit()
    row = (await db.execute(select(VisitaEvento))).scalar_one()
    assert row.payload_json is None
    assert row.user_id is None


async def test_log_visita_evento_truncates_oversized(db: AsyncSession) -> None:
    big = {"blob": "x" * 5000}
    await log_visita_evento(
        db,
        visita_id=db.info["visita_id"],
        tipo="eta_recalc",
        user_id=1,
        payload=big,
    )
    await db.commit()
    row = (await db.execute(select(VisitaEvento))).scalar_one()
    assert row.payload_json is not None
    assert len(row.payload_json.encode("utf-8")) <= 2000
    assert "_trunc" in row.payload_json

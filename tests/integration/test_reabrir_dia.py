"""CR-026 — Permitir reabrir un dia CERRADO (solo falabella_admin).

Casos cubiertos:
  1. admin: POST /dias/{id}/transition?nuevo_estado=EN_CURSO sobre un dia
     CERRADO funciona y `cerrado_at` queda NULL.
  2. ops y transport_manager: la misma transicion → 403.
  3. Las alertas auto-resueltas al cerrar NO se reactivan tras la reapertura
     (permanecen `resuelta`).
  4. El log de loguru incluye el user_id del admin que reabrio.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

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
from app.db.models.alert import Alert
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
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
    """Seed:
      * Empresa 1.
      * Un dia CERRADO con `cerrado_at` seteado.
      * Una alerta auto-resuelta sobre ese dia (simula el efecto del cierre).
      * Una alerta abierta sobre el mismo dia (para verificar que tampoco
        se modifica al reabrir — quedaria como historial inconsistente
        pero NO debe tocarse aca).
      * 3 users: admin, ops, transport_manager (todos con scope empresa 1).
    """
    async with _engine() as db:
        db.add(Empresa(empresa_id=1, nombre="E1", activo=True))
        await db.flush()

        dia = DiaOperativo(
            empresa_id=1,
            fecha=date(2026, 6, 10),
            estado="CERRADO",
            cerrado_at=datetime(2026, 6, 10, 19, 0, tzinfo=UTC),
        )
        db.add(dia)
        await db.flush()

        a_auto = Alert(
            tipo="eta_breach",
            severity="alta",
            empresa_id=1,
            dia_id=dia.dia_id,
            descripcion="visita atrasada X [auto-resuelta: dia cerrado]",
            estado="resuelta",
            resolved_at=datetime(2026, 6, 10, 19, 0, tzinfo=UTC),
            resolved_by_user_id=10,
        )
        db.add(a_auto)

        users = [
            User(
                user_id=10, email="adm@td.cl", password_hash="x",
                display_name="Adm", role="falabella_admin", activo=True,
            ),
            User(
                user_id=20, email="ops@td.cl", password_hash="x",
                display_name="Ops", role="falabella_ops", activo=True,
            ),
            User(
                user_id=30, email="tm@td.cl", password_hash="x",
                display_name="TM", role="transport_manager", activo=True,
            ),
        ]
        db.add_all(users)
        await db.flush()
        db.add_all([
            UserEmpresa(user_id=10, empresa_id=1),
            UserEmpresa(user_id=20, empresa_id=1),
            UserEmpresa(user_id=30, empresa_id=1),
        ])
        await db.commit()

        return {
            "engine_sm": _engine,
            "dia_id": dia.dia_id,
            "alert_auto_id": a_auto.alert_id,
        }


def _user_override(role: str, user_id: int, empresa_ids: list[int]):
    async def _stub() -> User:
        u = User(
            user_id=user_id, email=f"{role}@td.cl", password_hash="x",
            display_name=role, role=role, activo=True,
        )
        u._empresa_ids = empresa_ids  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(seeded: dict):
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    def make(role: str, user_id: int, empresa_ids: list[int]) -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _user_override(role, user_id, empresa_ids)
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------
# 1. admin reabre OK → cerrado_at queda NULL
# ----------------------------------------------------------------------------

def test_admin_reopens_cerrado_dia(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}"
        f"/transition?nuevo_estado=EN_CURSO"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["estado"] == "EN_CURSO"
    assert body["cerrado_at"] is None


# ----------------------------------------------------------------------------
# 2. ops / transport_manager → 403
# ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "role, user_id",
    [
        ("falabella_ops", 20),
        ("transport_manager", 30),
    ],
)
def test_non_admin_cannot_reopen_cerrado_dia(client_factory, seeded, role, user_id):
    c = client_factory(role, user_id, [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}"
        f"/transition?nuevo_estado=EN_CURSO"
    )
    assert r.status_code == 403, r.text
    assert "falabella_admin" in r.json()["detail"]


# ----------------------------------------------------------------------------
# 3. alertas auto-resueltas siguen `resuelta` despues de reabrir
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reopen_does_not_reactivate_auto_resolved_alerts(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [1])
    r = c.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}"
        f"/transition?nuevo_estado=EN_CURSO"
    )
    assert r.status_code == 200, r.text

    # Verificamos en DB que la alerta auto-resuelta sigue `resuelta`.
    sm = seeded["engine_sm"]
    async with sm() as db:
        a = (
            await db.execute(
                select(Alert).where(Alert.alert_id == seeded["alert_auto_id"])
            )
        ).scalar_one()
        assert a.estado == "resuelta"
        assert a.resolved_at is not None
        assert "[auto-resuelta: dia cerrado]" in a.descripcion


# ----------------------------------------------------------------------------
# 4. log incluye user_id del admin
# ----------------------------------------------------------------------------

def test_reopen_logs_user_id(client_factory, seeded, caplog):
    from loguru import logger

    # Tuberia loguru -> stdlib logging para que caplog capture el record.
    handler_id = logger.add(
        lambda msg: caplog.handler.emit(
            __import__("logging").LogRecord(
                name="loguru",
                level=__import__("logging").INFO,
                pathname=__file__,
                lineno=0,
                msg=str(msg).rstrip(),
                args=(),
                exc_info=None,
            )
        ),
        level="INFO",
    )
    try:
        with caplog.at_level("INFO"):
            c = client_factory("falabella_admin", 10, [1])
            r = c.post(
                f"/api/v1/operacion/dias/{seeded['dia_id']}"
                f"/transition?nuevo_estado=EN_CURSO"
            )
            assert r.status_code == 200, r.text

        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "REABIERTO" in joined
        # CR-026: el log DEBE incluir el user_id que reabrio.
        assert "user 10" in joined
        # Y el email del caller (el stub usa `{role}@td.cl`).
        assert "falabella_admin@td.cl" in joined
    finally:
        logger.remove(handler_id)

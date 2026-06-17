"""CR-3b end-of-day report push: correct template vars, contactos/usuarios only."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import app.core.report_push as rp
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.visita import Visita


@pytest_asyncio.fixture
async def seeded() -> AsyncIterator[tuple]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    now = datetime.now(UTC)
    async with sm() as s:
        s.add(Empresa(empresa_id=1, nombre="Empresa Uno", activo=True))
        await s.flush()
        dia = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 10), estado="CERRADO")
        s.add(dia)
        await s.flush()
        # 3 entregado + 1 no_entregado -> success 75%
        for est in ("entregado", "entregado", "entregado", "no_entregado"):
            s.add(Visita(dia_id=dia.dia_id, empresa_id=1, orden=1, cliente_nombre="c",
                         direccion="d", estado=est))
        # a contacto (should receive) + a driver (must NOT receive the report)
        s.add(EmpresaContacto(contact_id=1, empresa_id=1, nombre="Jefe", rol="jefe",
                              phone_e164="+56911111111", opted_in_at=now, activo=True))
        s.add(Driver(driver_id="D1", empresa_id=1, nombre="Ana", phone_e164="+56922222222",
                     opted_in_at=now, notify_whatsapp=True, activo=True))
        await s.commit()
        yield sm, dia.dia_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_push_sends_to_contacto_only_with_correct_vars(seeded, monkeypatch):
    sm, dia_id = seeded
    sent: list[dict] = []

    async def _fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(rp, "send_whatsapp", _fake_send)

    async with sm() as db:
        dia = (await db.execute(select(DiaOperativo).where(DiaOperativo.dia_id == dia_id))).scalar_one()
        n = await rp.push_dia_report(db, dia)

    assert n == 1, sent                       # only the contacto, not the driver
    cv = sent[0]["content_variables"]
    assert sent[0]["to"] == "+56911111111"
    assert sent[0]["content_sid"]             # REPORTE_DIA SID present
    assert cv["1"] == "Empresa Uno"
    assert cv["2"] == "2026-06-10"
    assert cv["3"] == "4"                     # visitas
    assert cv["4"] == "3"                     # entregadas
    assert cv["5"] == "75.0%"                 # exito


@pytest.mark.asyncio
async def test_push_noop_without_template(seeded, monkeypatch):
    sm, dia_id = seeded
    monkeypatch.setattr(rp, "reporte_dia_sid", lambda: "")
    async with sm() as db:
        dia = (await db.execute(select(DiaOperativo).where(DiaOperativo.dia_id == dia_id))).scalar_one()
        assert await rp.push_dia_report(db, dia) == 0

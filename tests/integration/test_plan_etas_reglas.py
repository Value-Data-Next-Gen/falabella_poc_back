"""CR-028 Part A — POST /api/v1/operacion/dias/{id}/plan-etas?respetar_reglas_cliente=...

Verifies:
  * Default behaviour (respetar_reglas_cliente=false) preserves the CR-019
    backwards-compatible shape: visitas_planificadas, shift_start, duracion_horas,
    no warnings, eta_estimada distributed evenly.
  * With reglas=true:
      - dias_no_disponible matches dia.fecha weekday → visita SKIPPED + warning;
        no eta_recalc audit row for that visita.
      - ventana_horaria clamps ETA to window start when planner would have set
        it earlier.
      - prioridad=1 + idx=0 → ETA forced to shift_start.
  * Each touched visita gets exactly one tipo='eta_recalc' audit row.
  * GET /visitas/{id}/eventos lists them.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, time

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
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient

# 2026-06-01 is a Monday → weekday code 'mon'.
DIA_FECHA = date(2026, 6, 1)


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
        db.add(Driver(driver_id="DRV-1", empresa_id=1, nombre="D", activo=True))
        await db.flush()
        d = DiaOperativo(empresa_id=1, fecha=DIA_FECHA, estado="VALIDADO")
        db.add(d)
        await db.flush()
        r = Ruta(dia_id=d.dia_id, driver_id="DRV-1", orden=1)
        db.add(r)
        await db.flush()

        # 4 clientes with different rules.
        c_blocked_mon = Cliente(
            nombre="Blocked", rut="R-B",
            es_vip=False, geocoding_status="pending", geocoding_attempts=0,
            dias_no_disponible='["mon"]',
        )
        c_window = Cliente(
            nombre="Window", rut="R-W",
            es_vip=False, geocoding_status="pending", geocoding_attempts=0,
            ventana_horaria_inicio=time(14, 0),
            ventana_horaria_fin=time(18, 0),
        )
        c_prio = Cliente(
            nombre="Prio", rut="R-P",
            es_vip=True, geocoding_status="pending", geocoding_attempts=0,
            prioridad=1,
        )
        c_plain = Cliente(
            nombre="Plain", rut="R-X",
            es_vip=False, geocoding_status="pending", geocoding_attempts=0,
        )
        db.add_all([c_blocked_mon, c_window, c_prio, c_plain])
        await db.flush()

        # 4 visitas:
        #   v_prio  orden=1, cliente_prio (high prio)
        #   v_block orden=2, cliente_blocked_mon (should be skipped)
        #   v_win   orden=3, cliente_window (window clamp)
        #   v_plain orden=4, cliente_plain (no rules)
        v_prio = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=1,
                        cliente_id=c_prio.cliente_id,
                        cliente_nombre="Prio", direccion="a", estado="pendiente")
        v_block = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=2,
                         cliente_id=c_blocked_mon.cliente_id,
                         cliente_nombre="Blocked", direccion="a", estado="pendiente")
        v_win = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=3,
                       cliente_id=c_window.cliente_id,
                       cliente_nombre="Window", direccion="a", estado="pendiente")
        v_plain = Visita(dia_id=d.dia_id, empresa_id=1, ruta_id=r.ruta_id, orden=4,
                         cliente_id=c_plain.cliente_id,
                         cliente_nombre="Plain", direccion="a", estado="pendiente")
        db.add_all([v_prio, v_block, v_win, v_plain])
        await db.commit()
        return {
            "engine_sm": _engine,
            "dia_id": d.dia_id,
            "ruta_id": r.ruta_id,
            "v_prio_id": v_prio.visita_id,
            "v_block_id": v_block.visita_id,
            "v_win_id": v_win.visita_id,
            "v_plain_id": v_plain.visita_id,
        }


def _override_admin():
    async def _stub() -> User:
        u = User(user_id=10, email="a@td.cl", password_hash="x",
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


async def test_plan_etas_default_backwards_compatible(client, seeded):
    """Default mode = CR-019 shape (no warnings, no rules applied)."""
    r = client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas"
        "?hora_inicio=9&duracion_horas=8"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visitas_planificadas"] == 4
    assert body["respetar_reglas_cliente"] is False
    assert body["warnings"] == []
    # All 4 visitas have eta_estimada now.
    async with seeded["engine_sm"]() as db:
        visitas = (
            await db.execute(select(Visita).where(Visita.ruta_id == seeded["ruta_id"]))
        ).scalars().all()
    assert all(v.eta_estimada is not None for v in visitas)


async def test_plan_etas_assigns_dense_orden(client, seeded):
    """plan-etas must persist a dense 1..N `orden` (was left 0 → 'Visita #0')."""
    # Reproduce the bug condition: visitas with no explicit orden.
    async with seeded["engine_sm"]() as db:
        for v in (await db.execute(
            select(Visita).where(Visita.ruta_id == seeded["ruta_id"])
        )).scalars().all():
            v.orden = 0
        await db.commit()

    r = client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas?hora_inicio=9&duracion_horas=8"
    )
    assert r.status_code == 200, r.text

    async with seeded["engine_sm"]() as db:
        ordens = sorted(
            v.orden for v in (await db.execute(
                select(Visita).where(Visita.ruta_id == seeded["ruta_id"])
            )).scalars().all()
        )
    assert ordens == [1, 2, 3, 4], f"expected dense 1..N orden, got {ordens}"


async def test_plan_etas_with_reglas_skips_blocked_day(client, seeded):
    r = client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas"
        "?hora_inicio=9&duracion_horas=8&respetar_reglas_cliente=true"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["respetar_reglas_cliente"] is True
    # 4 visitas total, 1 blocked → 3 planned + 1 warning.
    assert body["visitas_planificadas"] == 3
    assert len(body["warnings"]) == 1
    assert body["warnings"][0]["visita_id"] == seeded["v_block_id"]
    assert "mon" in body["warnings"][0]["reason"].lower()


async def test_plan_etas_with_reglas_window_clamp(client, seeded):
    """v_win has window 14-18; default 9-17 spacing puts orden=3 around ~15:00
    so no clamp; force a shorter shift so orden=3 lands before 14 → must clamp.
    """
    # shift = 9..15 (6h) → 4 visitas, gap = 1.5h → v_win at idx=2 lands at
    # 9 + 1.5*3 = 13:30, BEFORE window_inicio=14:00 → should clamp to 14:00.
    r = client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas"
        "?hora_inicio=9&duracion_horas=6&respetar_reglas_cliente=true"
    )
    assert r.status_code == 200, r.text
    async with seeded["engine_sm"]() as db:
        v_win = (
            await db.execute(select(Visita).where(Visita.visita_id == seeded["v_win_id"]))
        ).scalar_one()
    # eta_estimada hour must be >= 14.
    assert v_win.eta_estimada is not None
    assert v_win.eta_estimada.hour >= 14


async def test_plan_etas_with_reglas_high_priority_first_slot(client, seeded):
    r = client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas"
        "?hora_inicio=9&duracion_horas=8&respetar_reglas_cliente=true"
    )
    assert r.status_code == 200, r.text
    async with seeded["engine_sm"]() as db:
        v_prio = (
            await db.execute(select(Visita).where(Visita.visita_id == seeded["v_prio_id"]))
        ).scalar_one()
    # v_prio is the first visita and the cliente has prioridad=1 → ETA = shift_start = 09:00.
    assert v_prio.eta_estimada is not None
    assert v_prio.eta_estimada.hour == 9
    assert v_prio.eta_estimada.minute == 0


async def test_plan_etas_audit_log(client, seeded):
    client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas"
        "?hora_inicio=9&duracion_horas=8&respetar_reglas_cliente=true"
    )
    async with seeded["engine_sm"]() as db:
        # v_prio, v_win, v_plain each get one eta_recalc; v_block gets none.
        prio_events = (
            await db.execute(
                select(VisitaEvento).where(
                    VisitaEvento.visita_id == seeded["v_prio_id"],
                    VisitaEvento.tipo == "eta_recalc",
                )
            )
        ).scalars().all()
        block_events = (
            await db.execute(
                select(VisitaEvento).where(
                    VisitaEvento.visita_id == seeded["v_block_id"],
                    VisitaEvento.tipo == "eta_recalc",
                )
            )
        ).scalars().all()
    assert len(prio_events) == 1
    assert len(block_events) == 0


async def test_get_visita_eventos_lists_audit(client, seeded):
    client.post(
        f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas"
        "?hora_inicio=9&duracion_horas=8&respetar_reglas_cliente=true"
    )
    r = client.get(f"/api/v1/operacion/visitas/{seeded['v_prio_id']}/eventos")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) >= 1
    assert body[0]["tipo"] == "eta_recalc"
    assert "eta_new" in body[0]["payload"]


async def test_plan_etas_dia_cerrado_400(client, seeded):
    # Flip to CERRADO.
    async with seeded["engine_sm"]() as db:
        d = (
            await db.execute(select(DiaOperativo).where(DiaOperativo.dia_id == seeded["dia_id"]))
        ).scalar_one()
        d.estado = "CERRADO"
        await db.commit()
    r = client.post(f"/api/v1/operacion/dias/{seeded['dia_id']}/plan-etas")
    assert r.status_code == 400, r.text

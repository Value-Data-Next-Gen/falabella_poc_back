"""GET /api/v1/reports/dia/{dia_id} — day report + region/driver breakdown
+ day-over-day comparison + tenant scope."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest_asyncio
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
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
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
        poolclass=StaticPool, echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


async def _mk_visita(db, *, dia, ruta, estado, region, vip=False, motivo=None,
                     eta=None, done=None):
    v = Visita(
        dia_id=dia, empresa_id=1, ruta_id=ruta, orden=1,
        cliente_nombre="C", direccion="d", estado=estado, region=region,
        es_vip=1 if vip else 0, motivo=motivo, eta_estimada=eta, completada_at=done,
    )
    db.add(v)
    await db.flush()


@pytest_asyncio.fixture
async def seeded(_engine):
    base = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    async with _engine() as db:
        db.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        await db.flush()
        db.add(Driver(driver_id="DRV-01001", empresa_id=1, nombre="Ana", activo=True))
        # day1 = previous day (lower fecha); day2 = the report day.
        d1 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 9), estado="CERRADO")
        d2 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 10), estado="CERRADO")
        # a day for empresa 2 (for the cross-tenant test)
        d_other = DiaOperativo(empresa_id=2, fecha=date(2026, 6, 10), estado="CERRADO")
        db.add_all([d1, d2, d_other])
        await db.flush()
        r1 = Ruta(dia_id=d1.dia_id, driver_id="DRV-01001", orden=1)
        r2 = Ruta(dia_id=d2.dia_id, driver_id="DRV-01001", orden=1)
        db.add_all([r1, r2])
        await db.flush()

        # prev day: 1 entregado, 1 no_entregado → success 50%
        await _mk_visita(db, dia=d1.dia_id, ruta=r1.ruta_id, estado="entregado", region="RM")
        await _mk_visita(db, dia=d1.dia_id, ruta=r1.ruta_id, estado="no_entregado",
                         region="RM", motivo="SIN MORADORES")
        # report day: 3 entregado (1 VIP, 1 late), 1 no_entregado → success 75%
        await _mk_visita(db, dia=d2.dia_id, ruta=r2.ruta_id, estado="entregado", region="RM",
                         eta=base, done=base + timedelta(minutes=5))      # on time (grace 15)
        await _mk_visita(db, dia=d2.dia_id, ruta=r2.ruta_id, estado="entregado", region="RM",
                         eta=base, done=base + timedelta(minutes=40))     # late
        await _mk_visita(db, dia=d2.dia_id, ruta=r2.ruta_id, estado="entregado",
                         region="Valpo", vip=True)
        await _mk_visita(db, dia=d2.dia_id, ruta=r2.ruta_id, estado="no_entregado",
                         region="Valpo", motivo="CLIENTE RECHAZA")
        await db.commit()
        return {"engine_sm": _engine, "d1": d1.dia_id, "d2": d2.dia_id, "d_other": d_other.dia_id}


def _override_user(role, uid, empresa_ids):
    async def _stub() -> User:
        u = User(user_id=uid, email="x@td.cl", password_hash="x",
                 display_name=role, role=role, activo=True)
        u._empresa_ids = empresa_ids  # type: ignore[attr-defined]
        return u
    return _stub


@pytest.fixture
def client_factory(seeded):
    sm = seeded["engine_sm"]

    async def _get_db():
        async with sm() as s:
            yield s

    def make(role, uid, empresa_ids):
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[current_user] = _override_user(role, uid, empresa_ids)
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def test_dia_report_totals_and_breakdowns(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/reports/dia/{seeded['d2']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["visitas"] == 4
    assert body["totals"]["entregado"] == 3
    assert body["totals"]["no_entregado"] == 1
    assert body["totals"]["success_pct"] == 75.0
    # VIP subset: 1 entregado VIP
    assert body["vip"]["entregado"] == 1
    # on-time: 2 measured (eta+done), 1 on time (<=15m grace), 1 late
    assert body["on_time"]["medidas"] == 2
    assert body["on_time"]["a_tiempo"] == 1
    assert body["on_time"]["on_time_pct"] == 50.0
    # regions: RM (3) and Valpo (2)... RM has 3 visitas, Valpo 2 -> but only 4 total
    regions = {row["region"]: row for row in body["by_region"]}
    assert regions["RM"]["visitas"] == 2  # the two eta/done ones
    assert regions["Valpo"]["visitas"] == 2
    # driver behaviour
    drv = body["by_driver"][0]
    assert drv["driver_id"] == "DRV-01001"
    assert drv["nombre"] == "Ana"
    assert drv["visitas"] == 4
    # motivo
    assert {m["motivo"] for m in body["by_motivo"]} == {"CLIENTE RECHAZA"}


def test_dia_report_comparison_to_previous_day(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    body = c.get(f"/api/v1/reports/dia/{seeded['d2']}").json()
    comp = body["comparison"]
    assert comp["prev_dia_id"] == seeded["d1"]
    assert comp["visitas_delta"] == 2          # 4 vs 2
    assert comp["success_pct_delta"] == 25.0   # 75% vs 50%


def test_dia_report_first_day_has_no_comparison(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    body = c.get(f"/api/v1/reports/dia/{seeded['d1']}").json()
    assert body["comparison"]["prev_dia_id"] is None


def test_dia_report_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])  # scoped to empresa 1
    r = c.get(f"/api/v1/reports/dia/{seeded['d_other']}")  # empresa 2's day
    assert r.status_code == 403, r.text


def test_range_report_aggregates_both_days(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/reports/rango", params={
        "empresa_id": 1, "desde": "2026-06-09", "hasta": "2026-06-10"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dias"] == 2
    # d1: 1 entregado + 1 no_entregado; d2: 3 entregado + 1 no_entregado
    assert body["totals"]["visitas"] == 6
    assert body["totals"]["entregado"] == 4
    assert body["totals"]["no_entregado"] == 2
    assert body["totals"]["success_pct"] == round(100 * 4 / 6, 1)  # 66.7
    # punctuality only measurable on d2 (2 medidas, 1 on time)
    assert body["on_time"]["medidas"] == 2
    assert body["on_time"]["on_time_pct"] == 50.0
    # regions across both days: RM=4, Valpo=2
    regions = {row["region"]: row["visitas"] for row in body["by_region"]}
    assert regions["RM"] == 4
    assert regions["Valpo"] == 2
    # motivos across both: SIN MORADORES (d1) + CLIENTE RECHAZA (d2)
    assert {m["motivo"] for m in body["by_motivo"]} == {"SIN MORADORES", "CLIENTE RECHAZA"}


def test_range_report_trend_is_per_day_in_order(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    body = c.get("/api/v1/reports/rango", params={
        "empresa_id": 1, "desde": "2026-06-09", "hasta": "2026-06-10"}).json()
    trend = body["trend"]
    assert [p["fecha"] for p in trend] == ["2026-06-09", "2026-06-10"]
    assert trend[0]["visitas"] == 2 and trend[0]["success_pct"] == 50.0
    assert trend[1]["visitas"] == 4 and trend[1]["success_pct"] == 75.0
    assert trend[1]["on_time_pct"] == 50.0


def test_range_report_empty_window_is_zeroed(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    body = c.get("/api/v1/reports/rango", params={
        "empresa_id": 1, "desde": "2030-01-01", "hasta": "2030-01-31"}).json()
    assert body["dias"] == 0
    assert body["totals"]["visitas"] == 0
    assert body["trend"] == []
    assert body["by_region"] == []


def test_range_report_rejects_inverted_range(client_factory, seeded):
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/reports/rango", params={
        "empresa_id": 1, "desde": "2026-06-10", "hasta": "2026-06-09"})
    assert r.status_code == 400, r.text


def test_range_report_cross_tenant_403(client_factory, seeded):
    c = client_factory("transport_manager", 20, [1])  # scoped to empresa 1
    r = c.get("/api/v1/reports/rango", params={
        "empresa_id": 2, "desde": "2026-06-09", "hasta": "2026-06-10"})
    assert r.status_code == 403, r.text

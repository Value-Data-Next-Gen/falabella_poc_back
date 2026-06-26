"""CR-029 — integration tests for /api/v1/mapa/{visitas,heatmap,stats}.

Same in-memory SQLite pattern as `test_operacion_scope.py`:
  * Seed 2 empresas, 2 dias (same fecha), 2 rutas, mix of visitas across
    comunas + estados + VIPs.
  * Run as transport_manager scoped to empresa 1 → only sees their dia.
  * Run as admin → sees both empresas; can narrow with empresa_ids CSV.

Coverage:
  - test_mapa_visitas_admin_full
  - test_mapa_visitas_transport_manager_scope_silent
  - test_mapa_visitas_filter_estado_and_vip
  - test_mapa_visitas_atraso_min_computed_against_sim_clock
  - test_mapa_heatmap_aggregates_per_comuna
  - test_mapa_stats_global_and_per_empresa
  - test_mapa_stats_top_rutas_min_5_visitas
"""
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

os.environ["DB_TEST_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
from app.core.security import current_user
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient

FECHA = date(2026, 6, 1)
SIM_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


@pytest_asyncio.fixture(scope="function")
async def _engine() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy.pool import StaticPool

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
    """Seed the world for mapa tests.

    * empresa 1 ("Trans Uno"): dia1 with 1 ruta (10 visitas) — La Cisterna mix.
    * empresa 2 ("Trans Dos"): dia2 with 2 rutas (one with 6 visitas, one with 4)
      — Providencia mix + La Reina mix. The 4-visita ruta must NOT show up in
      top_rutas_best/worst (min 5).
    * SimClock set to SIM_NOW.
    """
    async with _engine() as db:
        e1 = Empresa(empresa_id=1, nombre="Trans Uno", activo=True)
        e2 = Empresa(empresa_id=2, nombre="Trans Dos", activo=True)
        db.add_all([e1, e2])
        await db.flush()

        v1 = Vehicle(vehicle_id=1, empresa_id=1, nombre="V1", plate="AAA11", activo=True)
        v2 = Vehicle(vehicle_id=2, empresa_id=2, nombre="V2", plate="BBB22", activo=True)
        db.add_all([v1, v2])
        await db.flush()

        d1 = Driver(driver_id="DRV-01001", empresa_id=1, nombre="Driver 1", activo=True)
        d2 = Driver(driver_id="DRV-02001", empresa_id=2, nombre="Driver 2", activo=True)
        d3 = Driver(driver_id="DRV-02002", empresa_id=2, nombre="Driver 3", activo=True)
        db.add_all([d1, d2, d3])
        await db.flush()

        dia1 = DiaOperativo(empresa_id=1, fecha=FECHA, estado="EN_CURSO")
        dia2 = DiaOperativo(empresa_id=2, fecha=FECHA, estado="EN_CURSO")
        db.add_all([dia1, dia2])
        await db.flush()

        # rutas with folio so top_rutas_* tests can assert the folio.
        ruta1 = Ruta(dia_id=dia1.dia_id, driver_id="DRV-01001", vehicle_id=1, folio="R-001", orden=1)
        ruta2 = Ruta(dia_id=dia2.dia_id, driver_id="DRV-02001", vehicle_id=2, folio="R-002", orden=1)
        ruta3 = Ruta(dia_id=dia2.dia_id, driver_id="DRV-02002", vehicle_id=2, folio="R-003", orden=2)
        db.add_all([ruta1, ruta2, ruta3])
        await db.flush()

        # --- Empresa 1 / ruta1 (10 visitas) -----------------------------------
        # 5 La Cisterna, 5 Providencia.
        # 4 entregadas, 2 no_entregadas, 4 pendientes (2 atrasadas).
        def make(
            dia_id, empresa_id, ruta_id, orden, comuna, estado, lat, lon,
            es_vip=False, eta=None, cliente="ACME", direccion="Av X 123",
            folio=None,
        ):
            return Visita(
                dia_id=dia_id, empresa_id=empresa_id, ruta_id=ruta_id, orden=orden,
                cliente_nombre=cliente, direccion=direccion, comuna=comuna,
                lat=lat, lon=lon, estado=estado, es_vip=1 if es_vip else 0,
                eta_estimada=eta, folio_cliente=folio,
            )

        early_eta = SIM_NOW - timedelta(hours=1)   # atrasada
        late_eta = SIM_NOW + timedelta(hours=1)    # on time

        e1_visitas = [
            make(dia1.dia_id, 1, ruta1.ruta_id, 1, "La Cisterna", "entregado", -33.53, -70.66, eta=early_eta, folio="F-001"),
            make(dia1.dia_id, 1, ruta1.ruta_id, 2, "La Cisterna", "entregado", -33.53, -70.66, eta=early_eta),
            make(dia1.dia_id, 1, ruta1.ruta_id, 3, "La Cisterna", "no_entregado", -33.53, -70.66, eta=early_eta),
            make(dia1.dia_id, 1, ruta1.ruta_id, 4, "La Cisterna", "pendiente", -33.53, -70.66, eta=early_eta),  # atrasada
            make(dia1.dia_id, 1, ruta1.ruta_id, 5, "La Cisterna", "pendiente", -33.53, -70.66, es_vip=True, eta=late_eta),
            make(dia1.dia_id, 1, ruta1.ruta_id, 6, "Providencia", "entregado", -33.43, -70.60, eta=early_eta),
            make(dia1.dia_id, 1, ruta1.ruta_id, 7, "Providencia", "entregado", -33.43, -70.60, eta=early_eta),
            make(dia1.dia_id, 1, ruta1.ruta_id, 8, "Providencia", "no_entregado", -33.43, -70.60, eta=early_eta),
            make(dia1.dia_id, 1, ruta1.ruta_id, 9, "Providencia", "pendiente", -33.43, -70.60, eta=early_eta),  # atrasada
            make(dia1.dia_id, 1, ruta1.ruta_id, 10, "Providencia", "pendiente", -33.43, -70.60, es_vip=True, eta=late_eta),
        ]

        # --- Empresa 2 / ruta2 (6 visitas, qualifies for top ranking) ---------
        # Best avance: 5 entregadas + 1 pendiente → 83.33%.
        e2_r2_visitas = [
            make(dia2.dia_id, 2, ruta2.ruta_id, 1, "La Reina", "entregado", -33.45, -70.54, eta=early_eta),
            make(dia2.dia_id, 2, ruta2.ruta_id, 2, "La Reina", "entregado", -33.45, -70.54, eta=early_eta),
            make(dia2.dia_id, 2, ruta2.ruta_id, 3, "La Reina", "entregado", -33.45, -70.54, eta=early_eta),
            make(dia2.dia_id, 2, ruta2.ruta_id, 4, "La Reina", "entregado", -33.45, -70.54, eta=early_eta),
            make(dia2.dia_id, 2, ruta2.ruta_id, 5, "La Reina", "entregado", -33.45, -70.54, eta=early_eta),
            make(dia2.dia_id, 2, ruta2.ruta_id, 6, "La Reina", "pendiente", -33.45, -70.54, eta=late_eta),
        ]

        # --- Empresa 2 / ruta3 (4 visitas, BELOW min for ranking) -------------
        e2_r3_visitas = [
            make(dia2.dia_id, 2, ruta3.ruta_id, 1, "Las Condes", "no_entregado", -33.42, -70.55, eta=early_eta),
            make(dia2.dia_id, 2, ruta3.ruta_id, 2, "Las Condes", "no_entregado", -33.42, -70.55, eta=early_eta),
            make(dia2.dia_id, 2, ruta3.ruta_id, 3, "Las Condes", "no_entregado", -33.42, -70.55, eta=early_eta),
            make(dia2.dia_id, 2, ruta3.ruta_id, 4, "Las Condes", "pendiente", -33.42, -70.55, eta=late_eta),
        ]

        db.add_all(e1_visitas + e2_r2_visitas + e2_r3_visitas)

        # SimClock so atraso_min is deterministic.
        db.add(SimClock(id=1, sim_now=SIM_NOW, speed=1.0, running=False, last_tick_at=SIM_NOW))

        admin = User(user_id=10, email="a@td.cl", password_hash="x", display_name="A", role="falabella_admin", activo=True)
        mgr = User(user_id=20, email="m@td.cl", password_hash="x", display_name="M", role="transport_manager", activo=True)
        db.add_all([admin, mgr])
        await db.flush()
        db.add(UserEmpresa(user_id=20, empresa_id=1))
        await db.commit()

        return {
            "engine_sm": _engine,
            "dia1_id": dia1.dia_id,
            "dia2_id": dia2.dia_id,
            "ruta1_id": ruta1.ruta_id,
            "ruta2_id": ruta2.ruta_id,
            "ruta3_id": ruta3.ruta_id,
        }


def _override_user(role: str, user_id: int, empresa_ids: list[int]):
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
    sessionmaker = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    def make(role: str, user_id: int, empresa_ids: list[int]) -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _override_user(role, user_id, empresa_ids)
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------
# Tests — /visitas
# ----------------------------------------------------------------------------

def test_mapa_visitas_admin_full(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/mapa/visitas?fecha={FECHA.isoformat()}")
    assert r.status_code == 200, r.text
    data = r.json()
    # 10 (e1) + 6 (e2.r2) + 4 (e2.r3) = 20.
    assert len(data) == 20
    # Both empresas represented.
    assert {row["empresa_id"] for row in data} == {1, 2}
    # Empresa names stitched.
    e1_row = next(r for r in data if r["empresa_id"] == 1)
    assert e1_row["empresa_nombre"] == "Trans Uno"
    # ruta_folio stitched.
    assert any(r["ruta_folio"] == "R-001" for r in data)


def test_mapa_visitas_transport_manager_scope_silent(client_factory):
    """transport_manager scoped to empresa 1 only sees their visitas, even when
    requesting empresa 2 explicitly via empresa_ids."""
    c = client_factory("transport_manager", 20, [1])
    r = c.get(f"/api/v1/mapa/visitas?fecha={FECHA.isoformat()}&empresa_ids=1,2")
    assert r.status_code == 200, r.text
    data = r.json()
    assert {row["empresa_id"] for row in data} == {1}
    assert len(data) == 10


def test_mapa_visitas_filter_estado_and_vip(client_factory):
    c = client_factory("falabella_admin", 10, [])
    # Only pendientes (incl. en_camino) + VIP.
    r = c.get(
        f"/api/v1/mapa/visitas?fecha={FECHA.isoformat()}"
        f"&estados=pendiente&solo_vip=true"
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # 2 VIPs pendientes in empresa 1.
    assert len(data) == 2
    assert all(row["es_vip"] for row in data)
    assert all(row["estado"] == "pendiente" for row in data)


def test_mapa_visitas_atraso_min_computed_against_sim_clock(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(
        f"/api/v1/mapa/visitas?fecha={FECHA.isoformat()}&estados=pendiente"
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # 6 pendientes total across both empresas:
    #   e1 (ruta1): 2 atrasadas (early_eta) + 2 on-time (late_eta) = 4
    #   e2 ruta2: 1 on-time (late_eta)
    #   e2 ruta3: 1 on-time (late_eta)
    # → atrasadas=2, on_time=4. Atrasadas have ~60 min of delay (sim_now - 1h).
    atrasadas = [r for r in data if (r["atraso_min"] or 0) >= 45]
    on_time = [r for r in data if r["atraso_min"] == 0]
    assert len(data) == 6
    assert len(atrasadas) == 2
    assert len(on_time) == 4
    # And the atrasadas should be ~60 minutes (allow ±5 for clock drift).
    assert all(55 <= r["atraso_min"] <= 65 for r in atrasadas)


def test_mapa_visitas_invalid_empresa_ids_returns_400(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/mapa/visitas?fecha={FECHA.isoformat()}&empresa_ids=abc")
    assert r.status_code == 400, r.text


# ----------------------------------------------------------------------------
# Tests — /heatmap
# ----------------------------------------------------------------------------

def test_mapa_heatmap_aggregates_per_comuna(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/mapa/heatmap?fecha={FECHA.isoformat()}")
    assert r.status_code == 200, r.text
    body = r.json()
    # La Cisterna, Providencia, La Reina, Las Condes — 4 comunas (all in
    # centroide table).
    comunas = {b["comuna"] for b in body["buckets"]}
    assert comunas == {"La Cisterna", "Providencia", "La Reina", "Las Condes"}
    # La Cisterna: 5 visitas, 2 entregadas, 1 no_entregada, 2 pendientes
    # (1 atrasada with eta in the past, 1 on time → en realidad ambas: idx 4 atrasada, idx 5 vip on-time).
    la_cisterna = next(b for b in body["buckets"] if b["comuna"] == "La Cisterna")
    assert la_cisterna["total"] == 5
    assert la_cisterna["entregadas"] == 2
    assert la_cisterna["no_entregadas"] == 1
    assert la_cisterna["pendientes"] == 2
    assert la_cisterna["atrasadas"] == 1
    # Centroid present.
    assert la_cisterna["lat"] < 0
    assert la_cisterna["lon"] < 0


def test_mapa_heatmap_scope_filters_to_manager(client_factory):
    c = client_factory("transport_manager", 20, [1])
    r = c.get(f"/api/v1/mapa/heatmap?fecha={FECHA.isoformat()}")
    assert r.status_code == 200
    comunas = {b["comuna"] for b in r.json()["buckets"]}
    # Only empresa 1 comunas.
    assert comunas == {"La Cisterna", "Providencia"}


# ----------------------------------------------------------------------------
# Tests — /stats
# ----------------------------------------------------------------------------

def test_mapa_stats_global_and_per_empresa(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/mapa/stats?fecha={FECHA.isoformat()}")
    assert r.status_code == 200, r.text
    body = r.json()

    # Global: 20 visitas, 9 entregadas (4 e1 + 5 e2.r2), 5 no_entregadas (2 e1 + 3 e2.r3).
    assert body["total_visitas"] == 20
    assert body["total_entregadas"] == 9
    assert body["total_no_entregadas"] == 5
    assert body["avance_pct"] == round((9 + 5) / 20 * 100, 2)

    # Per empresa.
    by_id = {p["empresa_id"]: p for p in body["por_empresa"]}
    assert by_id[1]["visitas"] == 10
    assert by_id[1]["entregadas"] == 4
    assert by_id[1]["no_entregadas"] == 2
    assert by_id[1]["rutas"] == 1
    assert by_id[2]["rutas"] == 2  # ruta2 + ruta3.
    assert by_id[2]["visitas"] == 10


def test_mapa_stats_top_rutas_min_5_visitas(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/mapa/stats?fecha={FECHA.isoformat()}")
    assert r.status_code == 200
    body = r.json()
    best_ids = [r["ruta_folio"] for r in body["top_rutas_best"]]
    worst_ids = [r["ruta_folio"] for r in body["top_rutas_worst"]]
    # ruta3 has 4 visitas → excluded from both rankings.
    assert "R-003" not in best_ids
    assert "R-003" not in worst_ids
    # ruta2 has 5/6 done (83.33%) — should be in best.
    assert "R-002" in best_ids


def test_mapa_stats_top_comunas_fails(client_factory):
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/mapa/stats?fecha={FECHA.isoformat()}")
    assert r.status_code == 200
    body = r.json()
    fails = {row["comuna"]: row["no_entregadas"] for row in body["top_comunas_fails"]}
    # Las Condes: 3 no_entregadas (ruta3); La Cisterna: 1; Providencia: 1.
    assert fails.get("Las Condes") == 3

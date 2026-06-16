"""CR-027 — Cliente master is identity-only; scope derives from visitas.

Verifies:
  * GET /api/v1/clientes paginated wrapper still works (shape: items/total/
    limit/offset) — preserved from CR-023.
  * transport_manager only sees clientes that have at least one visita in a
    dia of one of their empresas (no `cliente_empresas` table involved).
  * Multiple transport_managers in different empresas BOTH see a cliente
    shared by their respective visitas (no duplication, DISTINCT).
  * ClienteOut response no longer has `empresa_id` or `empresas_servidas`.
  * `visitas_total` is computed live, scoped to the caller.
  * POST /clientes accepts no `empresa_id` — body without it works; body with
    extra `empresa_id` field is silently ignored (Pydantic extra=ignore in
    ClienteBase? — actually fields default to "ignore" in v2 only when
    declared; here Pydantic will reject extra fields. We test by simply
    omitting it).
  * Historial-visitas and the new empresas-servidas endpoint work via the
    visitas join.
"""
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
from app.db import models  # noqa: F401  — registers all tables on Base.metadata
from app.db.base import Base
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.empresa import Empresa
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.models.visita import Visita
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


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
    """Seed scenario for CR-027 (no cliente_empresas table).

    - 2 empresas (1, 2).
    - 1 cliente "FAL-100" served by BOTH empresas (one visita in each).
    - 1 cliente "FAL-200" served only by empresa 1.
    - 2 dias_operativos (one per empresa) on the same date.
    """
    async with _engine() as db:
        db.add_all([
            Empresa(empresa_id=1, nombre="E1", activo=True),
            Empresa(empresa_id=2, nombre="E2", activo=True),
        ])
        await db.flush()

        day1 = DiaOperativo(empresa_id=1, fecha=date(2026, 6, 1), estado="EN_CURSO")
        day2 = DiaOperativo(empresa_id=2, fecha=date(2026, 6, 1), estado="EN_CURSO")
        db.add_all([day1, day2])
        await db.flush()

        c100 = Cliente(
            nombre="Cliente 100",
            rut="FAL-100",
            es_vip=False,
            geocoding_status="pending",
            geocoding_attempts=0,
        )
        c200 = Cliente(
            nombre="Cliente 200",
            rut="FAL-200",
            es_vip=True,
            vip_razon="frecuente",
            geocoding_status="pending",
            geocoding_attempts=0,
        )
        db.add_all([c100, c200])
        await db.flush()

        ruta1 = Ruta(dia_id=day1.dia_id, driver_id="DRV-01001", orden=1, folio="R1")
        ruta2 = Ruta(dia_id=day2.dia_id, driver_id="DRV-02001", orden=1, folio="R2")
        db.add_all([ruta1, ruta2])
        await db.flush()

        # Visitas: 100 in both empresas, 200 in empresa 1 only.
        v1 = Visita(
            ruta_id=ruta1.ruta_id, dia_id=day1.dia_id, empresa_id=1, orden=1,
            cliente_id=c100.cliente_id, cliente_nombre="Cliente 100",
            direccion="Calle 1 100", estado="entregado", folio_cliente="100",
        )
        v2 = Visita(
            ruta_id=ruta2.ruta_id, dia_id=day2.dia_id, empresa_id=2, orden=1,
            cliente_id=c100.cliente_id, cliente_nombre="Cliente 100",
            direccion="Calle 2 100", estado="no_entregado",
            motivo="cliente_ausente", folio_cliente="100",
        )
        v3 = Visita(
            ruta_id=ruta1.ruta_id, dia_id=day1.dia_id, empresa_id=1, orden=2,
            cliente_id=c200.cliente_id, cliente_nombre="Cliente 200",
            direccion="Calle 1 200", estado="entregado", folio_cliente="200",
        )
        db.add_all([v1, v2, v3])
        await db.flush()

        # Users.
        db.add_all([
            User(user_id=10, email="adm@td.cl", password_hash="x",
                 display_name="Adm", role="falabella_admin", activo=True),
            User(user_id=20, email="mgr1@td.cl", password_hash="x",
                 display_name="MgrE1", role="transport_manager", activo=True),
            User(user_id=30, email="mgr2@td.cl", password_hash="x",
                 display_name="MgrE2", role="transport_manager", activo=True),
        ])
        await db.flush()
        db.add_all([
            UserEmpresa(user_id=20, empresa_id=1),
            UserEmpresa(user_id=30, empresa_id=2),
        ])
        await db.commit()

        return {
            "engine_sm": _engine,
            "c100_id": c100.cliente_id,
            "c200_id": c200.cliente_id,
            "v1_id": v1.visita_id,
            "v2_id": v2.visita_id,
            "v3_id": v3.visita_id,
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
    sm = seeded["engine_sm"]

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    def make(role: str, user_id: int, empresa_ids: list[int]) -> TestClient:
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[current_user] = _override_user(
            role, user_id, empresa_ids
        )
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


# ──────────────────────────────────────────────────────────────────────
# Tests — shape & scope
# ──────────────────────────────────────────────────────────────────────


def test_list_returns_paginated_wrapper(client_factory, seeded):
    """Response shape: {items,total,limit,offset}."""
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/clientes?limit=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    assert "items" in body and "total" in body and "limit" in body and "offset" in body
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert body["total"] == 2  # FAL-100, FAL-200
    rutas = {item["rut"] for item in body["items"]}
    assert rutas == {"FAL-100", "FAL-200"}


def test_clienteout_has_no_empresa_id_or_empresas_servidas(client_factory, seeded):
    """CR-027 BREAKING: the response shape removed `empresa_id` and
    `empresas_servidas`.
    """
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "empresa_id" not in body, body
    assert "empresas_servidas" not in body, body
    assert "empresas_servidas_detalle" not in body, body
    # `visitas_total` remains.
    assert "visitas_total" in body


def test_admin_sees_all_visitas_in_total(client_factory, seeded):
    """falabella_admin: visitas_total counts across ALL empresas."""
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    # FAL-100 has 2 visitas (one per empresa).
    assert body["visitas_total"] == 2


def test_transport_manager_e1_sees_cliente_via_visita(client_factory, seeded):
    """mgr empresa 1 sees BOTH clientes (FAL-100 via v1, FAL-200 via v3)."""
    c = client_factory("transport_manager", 20, [1])
    r = c.get("/api/v1/clientes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {item["rut"] for item in body["items"]} == {"FAL-100", "FAL-200"}
    assert body["total"] == 2


def test_transport_manager_e2_sees_only_shared_cliente(client_factory, seeded):
    """mgr empresa 2 sees ONLY FAL-100 (via v2). FAL-200 has no visita in e2."""
    c = client_factory("transport_manager", 30, [2])
    r = c.get("/api/v1/clientes")
    assert r.status_code == 200, r.text
    body = r.json()
    rutas = {item["rut"] for item in body["items"]}
    assert rutas == {"FAL-100"}
    assert body["total"] == 1


def test_total_not_duplicated_by_distinct(client_factory, seeded):
    """Same cliente served by multiple empresas yields total=1 for an admin,
    not total=N. DISTINCT must be applied in the scope subquery.
    """
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/clientes")
    assert r.status_code == 200, r.text
    body = r.json()
    # 2 distinct clientes regardless of how many visitas exist.
    assert body["total"] == 2
    # And no duplicate items in the list.
    ids = [item["cliente_id"] for item in body["items"]]
    assert len(ids) == len(set(ids))


def test_visitas_total_scoped_for_manager(client_factory, seeded):
    """For mgr e2 looking at FAL-100, `visitas_total` only counts the e2 visita."""
    c = client_factory("transport_manager", 30, [2])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visitas_total"] == 1


def test_transport_manager_cannot_see_out_of_scope_detail(client_factory, seeded):
    """mgr empresa 2 cannot GET cliente FAL-200 (no visita of FAL-200 in e2)."""
    c = client_factory("transport_manager", 30, [2])
    r = c.get(f"/api/v1/clientes/{seeded['c200_id']}")
    assert r.status_code == 403, r.text


def test_historial_visitas_admin_sees_all(client_factory, seeded):
    """admin sees both visitas of FAL-100 (e1 + e2)."""
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}/historial-visitas")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    empresas = {item["empresa_id"] for item in body["items"]}
    assert empresas == {1, 2}


def test_historial_visitas_manager_scoped(client_factory, seeded):
    """mgr empresa 2 sees only the v2 visita of FAL-100 (not v1)."""
    c = client_factory("transport_manager", 30, [2])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}/historial-visitas")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["empresa_id"] == 2
    assert body["items"][0]["estado"] == "no_entregado"


def test_historial_visitas_out_of_scope_403(client_factory, seeded):
    """mgr empresa 2 cannot read history of cliente NOT visited in e2."""
    c = client_factory("transport_manager", 30, [2])
    r = c.get(f"/api/v1/clientes/{seeded['c200_id']}/historial-visitas")
    assert r.status_code == 403, r.text


def test_empresa_id_filter(client_factory, seeded):
    """?empresa_id=2 filters to clientes that have visitas in empresa 2."""
    c = client_factory("falabella_admin", 10, [])
    r = c.get("/api/v1/clientes?empresa_id=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["rut"] == "FAL-100"


# ──────────────────────────────────────────────────────────────────────
# Tests — empresas-servidas derived endpoint
# ──────────────────────────────────────────────────────────────────────


def test_empresas_servidas_admin_full(client_factory, seeded):
    """admin: GET /empresas-servidas for FAL-100 returns 2 rows (e1 + e2)."""
    c = client_factory("falabella_admin", 10, [])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}/empresas-servidas")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    by_emp = {row["empresa_id"]: row for row in body}
    assert set(by_emp.keys()) == {1, 2}
    assert by_emp[1]["visitas_count"] == 1
    assert by_emp[2]["visitas_count"] == 1


def test_empresas_servidas_manager_scoped(client_factory, seeded):
    """mgr e2: only sees e2 in the empresas-servidas projection."""
    c = client_factory("transport_manager", 30, [2])
    r = c.get(f"/api/v1/clientes/{seeded['c100_id']}/empresas-servidas")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {row["empresa_id"] for row in body} == {2}


# ──────────────────────────────────────────────────────────────────────
# Tests — POST without empresa_id
# ──────────────────────────────────────────────────────────────────────


def test_create_cliente_no_empresa_id(client_factory, seeded):
    """POST /clientes with NO empresa_id creates an identity-only cliente."""
    c = client_factory("falabella_admin", 10, [])
    r = c.post(
        "/api/v1/clientes",
        json={"nombre": "Nuevo Identidad", "rut": "FAL-999"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rut"] == "FAL-999"
    assert "empresa_id" not in body
    assert "empresas_servidas" not in body
    assert body["visitas_total"] == 0


def test_idempotent_re_request_does_not_duplicate(client_factory, seeded):
    """A second GET returns the SAME totals; nothing inflates between calls."""
    c = client_factory("falabella_admin", 10, [])
    r1 = c.get("/api/v1/clientes")
    r2 = c.get("/api/v1/clientes")
    assert r1.json()["total"] == r2.json()["total"]
    assert r1.json()["items"][0]["visitas_total"] == r2.json()["items"][0]["visitas_total"]


# ──────────────────────────────────────────────────────────────────────
# Tests — Ingest no longer creates cliente_empresas rows
# ──────────────────────────────────────────────────────────────────────


def test_cliente_empresas_table_does_not_exist():
    """Smoke: importing ClienteEmpresa must fail (model deleted in CR-027)."""
    with pytest.raises(ImportError):
        from app.db.models.cliente_empresa import ClienteEmpresa  # noqa: F401

"""Smoke tests del contrato: endpoints clave responden 200 con shape esperada.

Si alguno falla en CI, hay regresión del API. Lectura rápida del fallo:
- 200 → 5xx: backend tira excepción no manejada
- 200 → 4xx: cambió auth / validation / shape de query params
- 200 → 401: token vencido / endpoint marcó admin-required nuevo
"""
from __future__ import annotations


def test_health(get):
    r = get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_state_returns_today_and_vehicles(get):
    r = get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert "today" in body and "sim_clock" in body
    assert isinstance(body["vehicles"], list)


def test_auth_me_returns_admin(get):
    r = get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "falabella_admin"


def test_centros_distribucion_lists_active(get):
    r = get("/api/centros-distribucion")
    assert r.status_code == 200
    cds = r.json()
    assert isinstance(cds, list)
    assert len(cds) >= 1, "se esperaba al menos 1 CD activo"
    for cd in cds:
        assert {"cd_id", "region", "nombre", "lat", "lon"} <= set(cd.keys())


def test_plan_diario_real_returns_empresas_shape(get):
    r = get("/api/plan-diario", source="real")
    assert r.status_code == 200
    body = r.json()
    assert "empresas" in body
    assert isinstance(body["empresas"], list)


def test_plan_diario_legacy_returns_empresas_shape(get):
    """Regresión específica: el endpoint legacy referenciaba columnas
    inexistentes (planned_start/planned_end) y 500eaba silenciosamente,
    rompiendo el Probador IA. Si vuelve a 500 este test lo detecta."""
    r = get("/api/plan-diario", legacy="true", source="real", planned_date="2026-05-13")
    assert r.status_code == 200, f"legacy 5xx (regresión Probador IA): {r.status_code} {r.text[:200]}"
    body = r.json()
    assert "empresas" in body


def test_motivo_corrections_pending_list(get):
    r = get("/api/motivo-corrections", status="pending", limit=10)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_driver_positions_for_today(get):
    """No fallar si el día tiene 0 drivers — solo verificar contrato."""
    r = get("/api/operacion/driver-positions", fecha="2026-05-13")
    assert r.status_code == 200
    body = r.json()
    assert "drivers" in body and "sim_active" in body


def test_day_state_returns_state_enum(get):
    r = get("/api/planificacion/day-state", fecha="2026-05-13")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] in ("BORRADOR", "VALIDADO", "EN_CURSO", "CERRADO")

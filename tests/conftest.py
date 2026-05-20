"""Fixtures pytest compartidas.

Estos tests son **smoke tests** contra un backend ya corriendo en localhost:8001.
NO levantan el app; NO mockean BD. El objetivo es detectar regresiones obvias
del contrato API y de auth — no cobertura unitaria exhaustiva (esa vendrá
después con factories + sqlite in-memory).

Para correr:
    cd backend && pytest tests/ -v

Si el backend no responde, todos los tests fallan con un error claro.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import pytest
import requests


BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8001")
# Timeout alto porque hay endpoints lentos contra Azure SQL en su 1er hit
# (cache miss tarda ~17s antes del CR-004; ya cacheado responde en <1s).
TIMEOUT_SEC = float(os.environ.get("TEST_TIMEOUT_SEC", "60"))


def _wait_backend_ready(deadline_sec: float = 5.0) -> bool:
    """True si el backend responde 200 en /api/health antes del deadline."""
    end = time.time() + deadline_sec
    while time.time() < end:
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session", autouse=True)
def _backend_alive() -> None:
    """Falla la suite entera si el backend no está arriba."""
    if not _wait_backend_ready():
        pytest.exit(
            f"Backend no responde en {BASE_URL}. "
            f"Arrancalo con: cd backend && uvicorn main:app --port 8001",
            returncode=2,
        )


@pytest.fixture(scope="session")
def admin_token() -> str:
    """JWT del admin demo. Se cachea por sesión."""
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "admin@falabella.cl", "password": "admin123"},
        timeout=TIMEOUT_SEC,
    )
    assert r.status_code == 200, f"login admin falló: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def get(admin_headers):
    """GET autenticado como admin."""
    def _get(path: str, **params):
        return requests.get(
            f"{BASE_URL}{path}",
            headers=admin_headers,
            params=params,
            timeout=TIMEOUT_SEC,
        )
    return _get


@pytest.fixture
def post(admin_headers):
    """POST autenticado como admin."""
    def _post(path: str, json: Optional[dict] = None, **params):
        return requests.post(
            f"{BASE_URL}{path}",
            headers=admin_headers,
            json=json,
            params=params,
            timeout=TIMEOUT_SEC,
        )
    return _post


@pytest.fixture
def anon_get():
    """GET SIN auth (para tests de admin-required)."""
    def _get(path: str, **params):
        return requests.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT_SEC)
    return _get


@pytest.fixture
def anon_post():
    """POST SIN auth (para tests de admin-required)."""
    def _post(path: str, json: Optional[dict] = None, **params):
        return requests.post(f"{BASE_URL}{path}", json=json, params=params, timeout=TIMEOUT_SEC)
    return _post

"""Verifica que endpoints destructivos NO acepten requests sin token admin.

Detectado en QA review (2026-05-13): hay 4 endpoints que pueden borrar/regenerar
el plan del día. Si alguno deja de validar `require_admin`, un usuario común
podría perder data. Estos tests son la red de contención.
"""
from __future__ import annotations

import pytest


DESTRUCTIVE_ENDPOINTS = [
    # (method, path, params, body)
    ("POST", "/api/planificacion/day-state/reset", {"fecha": "2026-05-13"}, None),
    ("POST", "/api/planificacion/day-state/regenerate", {"fecha": "2026-05-13"}, None),
    ("POST", "/api/planificacion/day-state/clean-and-regenerate",
     {"fecha": "2026-05-13", "rows": 10}, None),
    ("POST", "/api/live-gen/toggle", None, {"enabled": False}),
    ("POST", "/api/live-gen/reset", None, None),
]


@pytest.mark.parametrize("method,path,params,body", DESTRUCTIVE_ENDPOINTS)
def test_destructive_requires_auth(anon_post, method, path, params, body):
    """Sin token → 401/403, NUNCA 200."""
    if method != "POST":
        pytest.skip("solo POST en esta tabla")
    r = anon_post(path, json=body, **(params or {}))
    assert r.status_code in (401, 403), (
        f"{path} aceptó request anónima ({r.status_code}). "
        f"Falta require_admin / current_user."
    )


def test_login_with_wrong_password_returns_401(anon_post):
    r = anon_post("/api/auth/login", json={"email": "admin@falabella.cl", "password": "wrong"})
    assert r.status_code == 401


def test_login_with_missing_email_returns_422(anon_post):
    r = anon_post("/api/auth/login", json={"password": "x"})
    assert r.status_code == 422

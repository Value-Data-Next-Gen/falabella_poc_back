"""Security headers are emitted on every response, and the session cookie's
Secure flag is config-driven (was hardcoded False)."""
from __future__ import annotations

import os

os.environ.setdefault("DB_SCHEMA", "")

import pytest
from app.core.security import jwt as jwtmod
from app.main import app
from fastapi.testclient import TestClient
from starlette.responses import Response


def test_security_headers_present():
    with TestClient(app) as c:
        r = c.get("/api/v1/health")
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "max-age=" in (r.headers.get("strict-transport-security") or "")
    assert "frame-ancestors" in (r.headers.get("content-security-policy") or "")
    assert r.headers.get("referrer-policy")


@pytest.mark.parametrize("secure", [True, False])
def test_session_cookie_secure_flag_is_config_driven(monkeypatch, secure):
    monkeypatch.setattr(jwtmod.settings, "cookie_secure", secure)
    resp = Response()
    jwtmod.set_session_cookie(resp, "tok123")
    set_cookie = resp.headers.get("set-cookie", "")
    assert ("secure" in set_cookie.lower()) is secure, set_cookie
    assert "httponly" in set_cookie.lower()  # always httpOnly

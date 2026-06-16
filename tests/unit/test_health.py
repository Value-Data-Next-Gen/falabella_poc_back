"""Smoke tests for health endpoints — shape + headers (state covered by test_lifespan)."""
from __future__ import annotations

from app.main import app
from fastapi.testclient import TestClient


def test_health_returns_shape_and_request_id_header() -> None:
    """Verifies the response SHAPE (not the ready flag — that's lifespan-dependent)."""
    client = TestClient(app)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["ready"], bool)
    assert body["version"]
    assert "X-Request-Id" in r.headers


def test_openapi_schema_has_health_paths() -> None:
    client = TestClient(app)
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "/api/v1/health" in spec["paths"]
    assert "/api/v1/ready" in spec["paths"]
    assert spec["paths"]["/api/v1/health"]["get"]["operationId"] == "getHealth"
    assert spec["paths"]["/api/v1/ready"]["get"]["operationId"] == "getReady"

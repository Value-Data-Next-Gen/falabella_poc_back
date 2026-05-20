"""CR-013 — Smoke tests del endpoint POST /api/copiloto/decisions.

Patrón: HTTP contra backend vivo en :8001 (ver conftest). NO mockea DB.
- happy path: admin POSTea una decisión válida, recibe {decision_id, created_at}
- intent inválido → 422 (Pydantic Literal rechaza valores fuera de la enum)
- sin auth → 401
"""
from __future__ import annotations


_VALID_PAYLOAD = {
    "suggestion_id": "cr013-test-suggestion",
    "intent": "ignore",
    "tracking_id": None,
    "payload": {"severity": "low", "test": True},
}


def test_copiloto_decision_happy_path(post):
    r = post("/api/copiloto/decisions", json=_VALID_PAYLOAD)
    assert r.status_code == 200, f"esperaba 200, got {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "decision_id" in body, body
    assert "created_at" in body, body
    assert isinstance(body["decision_id"], int) and body["decision_id"] > 0


def test_copiloto_decision_invalid_intent_returns_422(post):
    bad = dict(_VALID_PAYLOAD, intent="not_a_valid_intent")
    r = post("/api/copiloto/decisions", json=bad)
    assert r.status_code == 422, f"esperaba 422 por Literal mismatch, got {r.status_code}: {r.text[:300]}"


def test_copiloto_decision_without_auth_returns_401(anon_post):
    r = anon_post("/api/copiloto/decisions", json=_VALID_PAYLOAD)
    assert r.status_code in (401, 403), (
        f"sin token debe ser 401/403, got {r.status_code}: {r.text[:300]}"
    )

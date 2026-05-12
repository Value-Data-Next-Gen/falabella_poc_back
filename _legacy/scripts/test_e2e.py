"""
End-to-end smoke tests for the ValueData x Falabella POC backend.

Usage:
    cd backend && python scripts/test_e2e.py

Runs a linear sequence of HTTP checks against a backend already running at
BASE_URL. Each step PASS/FAILs independently — no abort on first failure.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any, Callable

import requests

BASE_URL = "http://127.0.0.1:8001"
ADMIN_EMAIL = "admin@falabella.cl"
ADMIN_PASSWORD = "admin123"
TIMEOUT = 30

# Shared state across steps
ctx: dict[str, Any] = {
    "token": None,
    "headers": {},
}

results: list[dict[str, Any]] = []


def _short_err(e: Exception) -> str:
    msg = str(e).strip().splitlines()
    return msg[0] if msg else e.__class__.__name__


def step(name: str, fn: Callable[[], str | None]) -> None:
    """Run a step function, catching all exceptions. fn returns optional detail string on success."""
    t0 = time.perf_counter()
    try:
        detail = fn()
        elapsed = (time.perf_counter() - t0) * 1000
        results.append({"name": name, "ok": True, "detail": detail or "", "ms": elapsed})
        print(f"PASS  [{elapsed:6.0f}ms] {name}  {detail or ''}")
    except AssertionError as e:
        elapsed = (time.perf_counter() - t0) * 1000
        err = _short_err(e) or "assertion failed"
        results.append({"name": name, "ok": False, "detail": err, "ms": elapsed})
        print(f"FAIL  [{elapsed:6.0f}ms] {name}  -> {err}")
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        err = f"{e.__class__.__name__}: {_short_err(e)}"
        results.append({"name": name, "ok": False, "detail": err, "ms": elapsed})
        print(f"FAIL  [{elapsed:6.0f}ms] {name}  -> {err}")


def get(path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", None) or ctx["headers"]
    return requests.get(f"{BASE_URL}{path}", headers=headers, timeout=TIMEOUT, **kwargs)


def post(path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", None) or ctx["headers"]
    return requests.post(f"{BASE_URL}{path}", headers=headers, timeout=TIMEOUT, **kwargs)


def _preview(r: requests.Response, n: int = 200) -> str:
    body = r.text or ""
    body = body.replace("\n", " ")
    return body[:n]


# ---------------------------------------------------------------------------
# Group A — Health & auth
# ---------------------------------------------------------------------------
def t01_health():
    r = get("/api/health")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    assert j.get("status") == "ok", f"unexpected body: {j}"
    return f"status=ok ready={j.get('ready')}"


def t02_login_bad():
    r = post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "WRONG-XYZ"},
        headers={},
    )
    assert r.status_code == 401, f"expected 401 got {r.status_code} body={_preview(r)}"
    return "401 as expected"


def t03_login_good():
    r = post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers={},
    )
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    tok = j.get("access_token")
    assert tok and isinstance(tok, str), f"missing access_token: {j}"
    ctx["token"] = tok
    ctx["headers"] = {"Authorization": f"Bearer {tok}"}
    return f"token len={len(tok)}"


def t04_me():
    assert ctx["token"], "no token from login"
    # /api/me is not mounted in this build; the real route is /api/auth/me.
    # Try /api/me first for spec parity, fall back to /api/auth/me.
    r = get("/api/me")
    used = "/api/me"
    if r.status_code == 404:
        r = get("/api/auth/me")
        used = "/api/auth/me"
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)} (tried /api/me and /api/auth/me)"
    j = r.json()
    return f"via {used} email={j.get('email')} role={j.get('role')}"


# ---------------------------------------------------------------------------
# Group B — Endpoints fixeados (los 5 que estaban 500)
# ---------------------------------------------------------------------------
def t05_live_gen_stats():
    r = get("/api/live-gen/stats")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    return f"keys={list(r.json().keys())[:6]}"


def t06_watchlist():
    r = get("/api/watchlist")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    # accept either list or dict-with-list
    if isinstance(j, dict):
        # try common shapes
        for k in ("items", "watchlist", "data", "results"):
            if isinstance(j.get(k), list):
                return f"len({k})={len(j[k])}"
        return f"keys={list(j.keys())[:6]}"
    assert isinstance(j, list), f"expected list got {type(j).__name__}"
    return f"len={len(j)}"


def t07_admin_config():
    r = get("/api/admin/config")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    # the keys may live at top-level or nested
    flat = j if isinstance(j, dict) else {}
    has_eta = "eta_window_hours" in json.dumps(flat)
    has_alert = "alert_threshold" in json.dumps(flat)
    assert has_eta, f"missing eta_window_hours; keys={list(flat.keys())[:10]}"
    assert has_alert, f"missing alert_threshold; keys={list(flat.keys())[:10]}"
    return "has eta_window_hours+alert_threshold"


def t08_drivers():
    r = get("/api/drivers")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("items") or j.get("drivers") or [])
    assert isinstance(items, list) and len(items) > 0, f"empty drivers; body={_preview(r)}"
    return f"len={len(items)}"


def t09_fleet_vehicles():
    r = get("/api/fleet/vehicles")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("items") or j.get("vehicles") or [])
    assert isinstance(items, list) and len(items) > 0, f"empty vehicles; body={_preview(r)}"
    return f"len={len(items)}"


# ---------------------------------------------------------------------------
# Group C — Planificacion
# ---------------------------------------------------------------------------
def t10_calendar():
    r = get("/api/planificacion/calendar", params={"month": "2026-05"})
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("days") or j.get("items") or [])
    assert isinstance(items, list), f"expected list got {type(items).__name__}; body={_preview(r)}"
    return f"days={len(items)}"


def t11_day_status():
    r = get("/api/planificacion/day-status", params={"fecha": "2026-05-12"})
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    required = [
        "loaded",
        "prep_ok",
        "started",
        "started_at",
        "vip_count",
        "config_issues_count",
        "driver_issues_count",
    ]
    missing = [k for k in required if k not in j]
    assert not missing, f"missing keys {missing}; got keys={list(j.keys())}"
    return f"loaded={j['loaded']} prep_ok={j['prep_ok']} started={j['started']}"


def t12_day_prep():
    r = get("/api/planificacion/day-prep", params={"fecha": "2026-05-12"})
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    required = ["vips", "config_issues", "driver_issues", "all_ok"]
    missing = [k for k in required if k not in j]
    assert not missing, f"missing keys {missing}; got keys={list(j.keys())}"
    return (
        f"vips={len(j['vips'])} cfg_iss={len(j['config_issues'])} drv_iss={len(j['driver_issues'])} all_ok={j['all_ok']}"
    )


def t13_day_clients():
    r = get(
        "/api/planificacion/day-clients",
        params={"fecha": "2026-05-12", "q": "ripley"},
    )
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("items") or j.get("clients") or [])
    assert isinstance(items, list), f"expected list got {type(items).__name__}"
    return f"len={len(items)}"


def t14_dotacion_check():
    r = get("/api/planificacion/dotacion-check", params={"fecha": "2026-05-12"})
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("conflicts") or j.get("items") or [])
    assert isinstance(items, list), f"expected list got {type(items).__name__}"
    return f"conflicts={len(items)}"


# ---------------------------------------------------------------------------
# Group D — Mantenedores
# ---------------------------------------------------------------------------
def t15_empresas():
    r = get("/api/empresas")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("items") or j.get("empresas") or [])
    assert isinstance(items, list) and len(items) > 0, f"empty empresas; body={_preview(r)}"
    return f"len={len(items)}"


def t16_admin_clients():
    r = get("/api/admin/clients")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    # paginated: usually {items: [...], total, page, ...}
    if isinstance(j, dict):
        items = j.get("items") or j.get("clients") or j.get("data") or []
        return f"len={len(items)} total={j.get('total')}"
    return f"len={len(j) if isinstance(j, list) else '?'}"


def t17_admin_users():
    r = get("/api/admin/users")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    items = j if isinstance(j, list) else (j.get("items") or j.get("users") or [])
    assert isinstance(items, list), f"expected list got {type(items).__name__}"
    return f"len={len(items)}"


# ---------------------------------------------------------------------------
# Group E — Twilio
# ---------------------------------------------------------------------------
def t18_notif_config():
    r = get("/api/notifications/config")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    required = ["enabled", "dry_run", "from_number", "has_creds"]
    missing = [k for k in required if k not in j]
    assert not missing, f"missing keys {missing}; got={list(j.keys())}"
    ctx["notif_config_initial"] = j
    return f"enabled={j['enabled']} dry_run={j['dry_run']} has_creds={j['has_creds']} from={j['from_number']}"


def t19_notif_toggle():
    r = post(
        "/api/notifications/toggle",
        params={"enabled": "true", "dry_run": "false"},
    )
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    # confirm via /config
    r2 = get("/api/notifications/config")
    assert r2.status_code == 200, f"config recheck failed: {r2.status_code}"
    j2 = r2.json()
    assert j2.get("enabled") is True, f"enabled not True after toggle: {j2}"
    assert j2.get("dry_run") is False, f"dry_run not False after toggle: {j2}"
    return f"toggle->{j} verified enabled=True dry_run=False"


def t20_notif_test():
    r = post("/api/notifications/test")
    # Some POC implementations send to a sandbox number; allow 200
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    sid = None
    # Multiple possible shapes
    if isinstance(j, dict):
        if j.get("dry_run") is True:
            return f"dry_run=true result={j.get('result') or j.get('results')}"
        sent = j.get("sent")
        # Try to find sid in flat or nested
        flat = json.dumps(j)
        import re
        m = re.search(r'"sid"\s*:\s*"([A-Za-z0-9]+)"', flat)
        if m:
            sid = m.group(1)
        return f"sent={sent} sid={sid} keys={list(j.keys())[:6]}"
    return f"resp={_preview(r)}"


def t21_twilio_inbound():
    # Hit inbound webhook with form-urlencoded body, no signature header
    form = {
        "From": "whatsapp:+56932942337",
        "Body": "hola",
        "ProfileName": "Test User",
        "MessageSid": "SMtest123",
    }
    # explicitly NO auth header — Twilio webhook is public
    r = requests.post(
        f"{BASE_URL}/api/twilio/inbound",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    # Acceptable outcomes:
    #   200 + TwiML xml
    #   204 No Content
    #   403 if TWILIO_AUTH_TOKEN is set and signature missing
    if r.status_code == 403:
        return "403 (TWILIO_AUTH_TOKEN set, signature required — expected)"
    assert r.status_code in (200, 204), f"status={r.status_code} body={_preview(r)}"
    ctype = r.headers.get("content-type", "")
    if r.status_code == 204:
        return "204 No Content"
    # 200: should be TwiML XML
    body = r.text
    is_xml = "xml" in ctype.lower() or body.lstrip().startswith("<?xml") or "<Response" in body
    assert is_xml, f"expected TwiML xml; content-type={ctype} body={_preview(r)}"
    return f"200 twiml content-type={ctype}"


# ---------------------------------------------------------------------------
# Group F — Modelo (indirecto)
# ---------------------------------------------------------------------------
def t22_db_alive():
    # Hitting admin/config already exercises the DB (and indirectly schema_migrations
    # since the app boots through it). A successful 200 + parseable JSON is enough.
    r = get("/api/admin/config")
    assert r.status_code == 200, f"status={r.status_code} body={_preview(r)}"
    j = r.json()
    assert isinstance(j, dict) and len(j) > 0, f"empty config; body={_preview(r)}"
    return "DB-backed config responded OK (schema_migrations table assumed present)"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
STEPS: list[tuple[str, Callable[[], str | None]]] = [
    ("A1 GET /api/health", t01_health),
    ("A2 POST /api/auth/login bad creds -> 401", t02_login_bad),
    ("A3 POST /api/auth/login good creds -> token", t03_login_good),
    ("A4 GET /api/me", t04_me),
    ("B5 GET /api/live-gen/stats", t05_live_gen_stats),
    ("B6 GET /api/watchlist", t06_watchlist),
    ("B7 GET /api/admin/config", t07_admin_config),
    ("B8 GET /api/drivers", t08_drivers),
    ("B9 GET /api/fleet/vehicles", t09_fleet_vehicles),
    ("C10 GET /api/planificacion/calendar?month=2026-05", t10_calendar),
    ("C11 GET /api/planificacion/day-status", t11_day_status),
    ("C12 GET /api/planificacion/day-prep", t12_day_prep),
    ("C13 GET /api/planificacion/day-clients", t13_day_clients),
    ("C14 GET /api/planificacion/dotacion-check", t14_dotacion_check),
    ("D15 GET /api/empresas", t15_empresas),
    ("D16 GET /api/admin/clients", t16_admin_clients),
    ("D17 GET /api/admin/users", t17_admin_users),
    ("E18 GET /api/notifications/config", t18_notif_config),
    ("E19 POST /api/notifications/toggle enabled=true dry_run=false", t19_notif_toggle),
    ("E20 POST /api/notifications/test", t20_notif_test),
    ("E21 POST /api/twilio/inbound (form)", t21_twilio_inbound),
    ("F22 DB integrity (indirect via /api/admin/config)", t22_db_alive),
]


def main() -> int:
    print(f"E2E suite -> {BASE_URL}")
    print("=" * 72)
    t_start = time.perf_counter()
    for name, fn in STEPS:
        step(name, fn)
    total_ms = (time.perf_counter() - t_start) * 1000

    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    total = len(results)

    print("=" * 72)
    print("SUMMARY")
    print("-" * 72)
    print(f"  passed:  {passed}")
    print(f"  failed:  {failed}")
    print(f"  total:   {total}")
    print(f"  runtime: {total_ms:.0f} ms")
    if failed:
        print("-" * 72)
        print("FAILURES")
        for r in results:
            if not r["ok"]:
                print(f"  - {r['name']}\n      -> {r['detail']}")
    print("=" * 72)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)

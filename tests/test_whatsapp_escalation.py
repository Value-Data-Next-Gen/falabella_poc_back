"""CR-013 — Smoke tests del endpoint POST /api/whatsapp/escalate-supervisor.

Patrón HTTP contra backend vivo (igual que el resto de la suite). NO podemos
monkeypatchear Twilio desde acá; en cambio confiamos en que el backend está
arrancado con NOTIFICATIONS_DRY_RUN=true o sin creds Twilio reales — el
endpoint cae a dry_run automáticamente y NO golpea Twilio.

Tests:
  - sin auth → 401
  - tracking_id inexistente → 404
  - body inválido → 422
  - happy path: pickeo una visita real, seteo supervisor_phone en su empresa,
    POSTeo, verifico 200 + dispatch row insertada. Cooldown in-memory: si la
    suite se re-corre antes de los 300s default sin reiniciar backend, este
    test puede ver 429 — lo aceptamos como "cooldown válido" y se skipea el
    assert de DB en ese caso (la rama success ya fue cubierta en la corrida
    anterior). Para forzar success siempre, arrancá uvicorn con
    AUTO_NOTIFY_COOLDOWN_SEC=0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Acceso directo a la DB para verificar persistencia. Los tests corren desde
# backend/, así que `core.db` está en sys.path normalmente — defensivo igual.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Cargar el .env (raíz del repo o backend/) ANTES de importar core.db en los
# helpers, para que el test apunte al mismo backend DB que el uvicorn server
# (típicamente DB_BACKEND=sqlserver con creds Azure). Mismo pattern que
# main.py:27-31 y fpoc_loader/migrate_*.py.
for _p in (_BACKEND_DIR / ".env", _BACKEND_DIR.parent / ".env"):
    if _p.exists():
        load_dotenv(_p, override=False)
        break


def test_escalate_without_auth_returns_401(anon_post):
    r = anon_post(
        "/api/whatsapp/escalate-supervisor",
        json={"tracking_id": "no-existe-cr013"},
    )
    assert r.status_code in (401, 403), (
        f"sin token debe ser 401/403, got {r.status_code}: {r.text[:300]}"
    )


def test_escalate_with_nonexistent_tracking_returns_404(post):
    """tracking_id inventado → 404 (la visita no se resuelve)."""
    r = post(
        "/api/whatsapp/escalate-supervisor",
        json={"tracking_id": "non-existent-cr013-xyz-0000"},
    )
    assert r.status_code == 404, (
        f"esperaba 404 para tracking inexistente, got {r.status_code}: "
        f"{r.text[:300]}"
    )


def test_escalate_invalid_body_returns_422(post):
    """Body sin tracking_id → 422 (Pydantic rechaza)."""
    r = post("/api/whatsapp/escalate-supervisor", json={})
    assert r.status_code == 422, (
        f"esperaba 422 por falta de tracking_id, got {r.status_code}: "
        f"{r.text[:300]}"
    )


def _pick_real_visit(get) -> tuple[str, int]:
    """Devuelve (tracking_id, empresa_id) de una visita real del plan-diario.

    Pickea la primera visita de la primera ruta de la primera empresa que tenga
    al menos un tracking_id. Falla el test si no hay datos vivos."""
    r = get("/api/plan-diario", source="real")
    assert r.status_code == 200, f"plan-diario falló: {r.status_code}"
    data = r.json()
    empresas = data.get("empresas") or []
    for emp in empresas:
        emp_id = emp.get("empresa_id")
        for ruta in emp.get("rutas") or []:
            for v in ruta.get("visitas") or []:
                tid = v.get("tracking_id") or v.get("id")
                if tid and emp_id is not None:
                    return str(tid), int(emp_id)
    pytest.skip("No hay visitas reales en plan-diario para testear happy path")


def _set_supervisor_phone(empresa_id: int, phone: str | None) -> str | None:
    """UPDATE fpoc.empresas_transporte.supervisor_phone_e164. Devuelve el valor
    previo (para restaurarlo en teardown)."""
    from core.db import get_conn
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT supervisor_phone_e164 FROM fpoc.empresas_transporte "
            "WHERE empresa_id = ?",
            empresa_id,
        )
        row = cur.fetchone()
        prev = row[0] if row else None
        cur.execute(
            "UPDATE fpoc.empresas_transporte SET supervisor_phone_e164 = ? "
            "WHERE empresa_id = ?",
            phone, empresa_id,
        )
        cn.commit()
    return prev


def _count_dispatches(tracking_id: str) -> int:
    from core.db import get_conn
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM fpoc.alert_dispatch_log "
            "WHERE tracking_id = ? AND type = 'driver_sin_respuesta' "
            "AND target = 'supervisor'",
            tracking_id,
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _latest_dispatch_payload(tracking_id: str) -> dict | None:
    from core.db import get_conn
    with get_conn() as cn:
        cur = cn.cursor()
        # SQL Server-compatible: ORDER BY DESC + LIMIT 1 (el rewriter lo traduce).
        cur.execute(
            "SELECT payload_json FROM fpoc.alert_dispatch_log "
            "WHERE tracking_id = ? AND type = 'driver_sin_respuesta' "
            "AND target = 'supervisor' "
            "ORDER BY alert_id DESC LIMIT 1",
            tracking_id,
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None


def test_escalate_happy_path_inserts_dispatch(post, get):
    """Happy path completo: visita real + supervisor_phone seteado →
    200 + INSERT en alert_dispatch_log con payload.alert_type='escalation_supervisor'.
    """
    tracking_id, empresa_id = _pick_real_visit(get)

    # Setup: garantizar que el empresa tenga supervisor_phone configurado.
    test_phone = "+56900000001"
    prev_phone = _set_supervisor_phone(empresa_id, test_phone)
    count_before = _count_dispatches(tracking_id)

    try:
        r = post(
            "/api/whatsapp/escalate-supervisor",
            json={"tracking_id": tracking_id},
        )

        # 429 = cooldown todavía caliente de una corrida previa en el mismo
        # backend process. Aceptable: la rama success ya fue cubierta antes.
        if r.status_code == 429:
            pytest.skip(
                "Cooldown anti-spam activo (corrida previa <300s). "
                "Reiniciá uvicorn o seteá AUTO_NOTIFY_COOLDOWN_SEC=0 para "
                "forzar happy path."
            )

        assert r.status_code == 200, (
            f"esperaba 200 happy path, got {r.status_code}: {r.text[:500]}"
        )
        body = r.json()
        assert "dispatch_id" in body and isinstance(body["dispatch_id"], int), body
        assert body["dispatch_id"] > 0, body
        assert "sent_at" in body, body
        assert "dry_run" in body and isinstance(body["dry_run"], bool), body

        # Verificar el INSERT en alert_dispatch_log.
        count_after = _count_dispatches(tracking_id)
        assert count_after == count_before + 1, (
            f"esperaba +1 dispatch row, antes={count_before} despues={count_after}"
        )

        payload = _latest_dispatch_payload(tracking_id)
        assert payload is not None, "payload_json vacío o no-JSON en la última fila"
        assert payload.get("alert_type") == "escalation_supervisor", (
            f"alert_type fino debe ser 'escalation_supervisor', got {payload!r}"
        )
        # to debe coincidir con el phone que seteamos.
        assert payload.get("to") == test_phone, (
            f"to en payload debe ser el phone configurado, got {payload.get('to')!r}"
        )
    finally:
        # Teardown: restaurar valor previo de supervisor_phone (puede ser None).
        _set_supervisor_phone(empresa_id, prev_phone)

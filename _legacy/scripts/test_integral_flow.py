"""Test integral: carga XLSX → visitas → comentarios → audit IA.

Flujo:
  1. Login admin
  2. Subir datos_eta_2026_04_19.xlsx con force=true
  3. Verificar calendar muestra el día con visitas
  4. day-status confirma loaded=true y count correcto
  5. day-prep estructura completa
  6. Tomar 1 tracking_id real del día
  7. Listar comentarios actuales de esa visita (probablemente vacío)
  8. Agregar comentario via POST /api/visits/{tid}/comment
  9. Re-listar — debe aparecer
 10. /api/comments/recent — el comentario nuevo debe estar en el stream
 11. Marcar al cliente de esa visita como VIP
 12. day-prep verifica que VIP aparezca

Backend en 127.0.0.1:8001. Login: admin@falabella.cl / admin123.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8001"
XLSX_PATH = Path(__file__).resolve().parents[2] / "client" / "data" / "datos_eta_2026_04_19.xlsx"
FECHA = "2026-04-19"

# Resultados
results: list[tuple[str, bool, str]] = []


def step(name: str, fn):
    t0 = time.time()
    try:
        msg = fn()
        results.append((name, True, msg or "ok"))
        print(f"  [PASS] {name}  ({(time.time()-t0)*1000:.0f}ms) — {msg or ''}")
    except AssertionError as e:
        results.append((name, False, f"assert: {e}"))
        print(f"  [FAIL] {name} — assert: {e}")
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"{type(e).__name__}: {str(e)[:200]}"))
        print(f"  [FAIL] {name} — {type(e).__name__}: {str(e)[:200]}")


def main() -> None:
    print(f"\n=== Integration test contra {BASE} ===\n")

    # ---- 1. Login ----
    token = {"v": None}

    def login():
        r = requests.post(f"{BASE}/api/auth/login",
                          json={"email": "admin@falabella.cl", "password": "admin123"},
                          timeout=10)
        assert r.status_code == 200, f"login HTTP {r.status_code}"
        token["v"] = r.json()["access_token"]
        return f"token len={len(token['v'])}"
    step("1. login admin", login)

    if not token["v"]:
        print("\n[ABORT] sin token, no se puede continuar")
        sys.exit(1)

    H = {"Authorization": f"Bearer {token['v']}"}

    # ---- 2. Upload XLSX ----
    def upload_xlsx():
        assert XLSX_PATH.exists(), f"no encuentro {XLSX_PATH}"
        with XLSX_PATH.open("rb") as f:
            files = {"file": (XLSX_PATH.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            r = requests.post(
                f"{BASE}/api/planificacion/import-xlsx?force=true",
                headers=H, files=files, timeout=120,
            )
        assert r.status_code == 200, f"upload HTTP {r.status_code}: {r.text[:300]}"
        j = r.json()
        assert j.get("ok"), f"ok=false: {j.get('message')}"
        return f"simpli={j.get('simpli_count')} geo={j.get('geo_count')} fechas={j.get('fechas')}"
    step("2. upload datos_eta_2026_04_19.xlsx force=true", upload_xlsx)

    # ---- 3. Calendar muestra el día ----
    def calendar_check():
        r = requests.get(f"{BASE}/api/planificacion/calendar?month=2026-04", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        days = r.json()
        d = next((x for x in days if x["fecha"] == FECHA), None)
        assert d is not None, f"{FECHA} no aparece en el calendar"
        assert d["visitas"] > 0, f"visitas=0 para {FECHA}"
        return f"visitas={d['visitas']} pending={d['pending']} completed={d['completed']}"
    step("3. calendar 2026-04 incluye 2026-04-19", calendar_check)

    # ---- 4. day-status ----
    day_status = {"v": None}

    def day_status_check():
        r = requests.get(f"{BASE}/api/planificacion/day-status?fecha={FECHA}", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        s = r.json()
        day_status["v"] = s
        assert s["loaded"], "loaded=false después de upload"
        assert s["visitas"] > 0, "visitas=0"
        # Como recién subimos, started_at debería ser None
        return f"visitas={s['visitas']} prep_ok={s['prep_ok']} vips={s['vip_count']} cfg={s['config_issues_count']} drv={s['driver_issues_count']} started={s['started']}"
    step("4. day-status estructura completa", day_status_check)

    # ---- 5. day-prep ----
    day_prep = {"v": None}

    def day_prep_check():
        r = requests.get(f"{BASE}/api/planificacion/day-prep?fecha={FECHA}", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        p = r.json()
        day_prep["v"] = p
        assert "vips" in p and "config_issues" in p and "driver_issues" in p, "claves faltantes"
        return f"vips={len(p['vips'])} cfg={len(p['config_issues'])} drv={len(p['driver_issues'])} all_ok={p['all_ok']}"
    step("5. day-prep estructura", day_prep_check)

    # ---- 6. Tomar un tracking real del día ----
    tracking = {"v": None, "cliente": None, "vehicle_id": None}

    def pick_tracking():
        # Usamos plan-diario para sacar un tracking_id con vehicle_id real
        r = requests.get(f"{BASE}/api/plan-diario?planned_date={FECHA}", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        plan = r.json()
        empresas = plan.get("empresas", [])
        assert empresas, "plan-diario sin empresas"
        for emp in empresas:
            for ruta in emp.get("rutas", []):
                vis = ruta.get("visitas") or []
                if vis:
                    v = vis[0]
                    tracking["v"] = v["tracking_id"]
                    # title es lo que se guarda en simpli_visits.title; cliente_nombre
                    # puede ser un alias UI. Para el match de VIP usamos title.
                    tracking["cliente"] = v.get("title") or v.get("cliente_nombre")
                    tracking["vehicle_id"] = v.get("vehicle_id")
                    return f"tid={tracking['v']} title={tracking['cliente']!r}"
        raise AssertionError("ninguna ruta tiene visitas")
    step("6. seleccionar tracking real", pick_tracking)

    # ---- 7. Listar comentarios actuales ----
    def list_comments_pre():
        if not tracking["v"]:
            raise AssertionError("sin tracking, salteado")
        r = requests.get(f"{BASE}/api/visits/{tracking['v']}/comments", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
        return f"comentarios previos={len(r.json())}"
    step("7. GET comentarios de la visita (pre)", list_comments_pre)

    # ---- 8. Agregar comentario ----
    new_comment_id = {"v": None}

    def add_comment():
        if not tracking["v"]:
            raise AssertionError("sin tracking")
        payload = {
            "motivo": "SIN MORADORES",
            "comentario": "TEST INTEGRAL — cliente no respondió, dejé aviso por timbre",
        }
        r = requests.post(
            f"{BASE}/api/visits/{tracking['v']}/comment",
            headers={**H, "Content-Type": "application/json"},
            json=payload, timeout=30,
        )
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
        c = r.json()
        new_comment_id["v"] = c.get("comment_id")
        return f"comment_id={new_comment_id['v']} motivo={c.get('motivo')!r}"
    step("8. POST agregar comentario", add_comment)

    # ---- 9. Re-listar — comentario debe aparecer ----
    def list_comments_post():
        if not tracking["v"]:
            raise AssertionError("sin tracking")
        r = requests.get(f"{BASE}/api/visits/{tracking['v']}/comments", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        coms = r.json()
        assert any("TEST INTEGRAL" in (c.get("comentario") or "") for c in coms), \
            "comentario nuevo no aparece en el listado"
        return f"comentarios ahora={len(coms)}"
    step("9. GET comentarios incluye el nuevo", list_comments_post)

    # ---- 10. /api/comments/recent debe traer el comment ----
    def recent_includes_new():
        r = requests.get(f"{BASE}/api/comments/recent?limit=20", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        coms = r.json()
        found = any("TEST INTEGRAL" in (c.get("comentario") or "") for c in coms)
        assert found, f"comentario nuevo no aparece en /comments/recent (top {len(coms)})"
        return f"recent total={len(coms)}"
    step("10. /comments/recent contiene el nuevo", recent_includes_new)

    # ---- 11. Marcar cliente como VIP ----
    vip_created = {"v": None}

    def mark_vip():
        cliente = tracking.get("cliente") or "TEST_CLIENT"
        payload = {
            "match_type": "title", "match_value": cliente,
            "tier": "VIP",
            "notes": f"Marcado desde test integral {FECHA}",
        }
        r = requests.post(
            f"{BASE}/api/vip-clients",
            headers={**H, "Content-Type": "application/json"},
            json=payload, timeout=30,
        )
        # Puede dar 200 (creado) o 409 (ya existe) — ambos aceptables
        if r.status_code == 409:
            return f"ya existía VIP para {cliente!r}"
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
        vip_created["v"] = r.json().get("vip_id")
        return f"vip_id={vip_created['v']} cliente={cliente!r}"
    step("11. POST /vip-clients (cliente del tracking)", mark_vip)

    # ---- 12. day-prep refleja el VIP ----
    def day_prep_post():
        r = requests.get(f"{BASE}/api/planificacion/day-prep?fecha={FECHA}", headers=H, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        p = r.json()
        cliente = tracking.get("cliente")
        if cliente:
            has_it = any(v["cliente"] == cliente for v in p["vips"])
            assert has_it, f"VIP {cliente!r} no aparece en day-prep.vips (total VIPs={len(p['vips'])})"
        return f"vips post={len(p['vips'])} all_ok={p['all_ok']}"
    step("12. day-prep refleja el VIP recién creado", day_prep_post)

    # ---- Resumen ----
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    print(f"\n=== {passed}/{total} PASS — {failed} FAIL ===\n")
    if failed:
        print("FAILS:")
        for name, ok, msg in results:
            if not ok:
                print(f"  - {name}: {msg}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

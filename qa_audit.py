"""QA suite: validacion end-to-end de la app POC.
Severidad: BLOCKER / CRITICAL / MAJOR / MINOR / INFO.
"""
import json
import sys
import time
import sqlite3
from datetime import date

import requests

BASE = "http://localhost:8001/api"
DB = "valuedata.db"

findings = []


def F(sev, area, msg, evidence=None):
    findings.append({"sev": sev, "area": area, "msg": msg, "evidence": evidence})


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# 1. Health
section("1. SMOKE - backend health")
r = requests.get(f"{BASE}/health")
print(f"  /health -> {r.status_code} {r.json()}")
if r.status_code != 200 or not r.json().get("ready"):
    F("BLOCKER", "infra", "backend no ready", r.text)
    sys.exit(1)

# 2. Auth
section("2. AUTH - login")
r = requests.post(f"{BASE}/auth/login", json={"email": "admin@falabella.cl", "password": "admin123"})
if r.status_code != 200:
    F("BLOCKER", "auth", f"login admin falla: {r.status_code}", r.text)
    sys.exit(1)
TOK = r.json()["access_token"]
H = {"Authorization": f"Bearer {TOK}"}
print("  login admin OK")

# 3. Endpoints
section("3. ENDPOINTS - coverage")
endpoints = [
    "/state", "/visits", "/visits?region=RM", "/visits?region=regiones",
    "/alerts/anticipated", "/kpis", "/vehicles", "/drivers",
    "/drivers/scorecard", "/plan-diario?legacy=true",
    "/planificacion/imports", "/admin/config", "/notifications/config",
    "/notifications/log?limit=10", "/notifications/log?direction=inbound&limit=5",
    "/twilio/inbound/test", "/whatsapp/onboard/list",
    "/whatsapp/onboard/sandbox-info", "/motivos/system-prompt",
]
for p in endpoints:
    try:
        r = requests.get(f"{BASE}{p}", headers=H, timeout=15)
        flag = "OK" if r.status_code == 200 else "FAIL"
        print(f"  [{flag}] GET {p:<55s} -> {r.status_code}")
        if r.status_code != 200:
            F("CRITICAL", "endpoints", f"GET {p} -> {r.status_code}", r.text[:200])
    except Exception as e:
        print(f"  [ERR] GET {p}: {e}")
        F("CRITICAL", "endpoints", f"GET {p} ex: {e}")

# 4. Coherencia visits
section("4. COHERENCIA - visitas / regiones / drivers")
visits = requests.get(f"{BASE}/visits", headers=H).json()
print(f"  /visits total: {len(visits)}")
rm_count = sum(1 for v in visits if v.get("region") == "RM")
non_rm = len(visits) - rm_count
print(f"  RM: {rm_count} ({100 * rm_count // max(1, len(visits))}%) | regiones: {non_rm}")
if rm_count == 0:
    F("CRITICAL", "data", "snapshot sin visitas RM")
if non_rm == 0:
    F("MAJOR", "data", "snapshot sin visitas en regiones (esperado ~17%)")

cn = sqlite3.connect(DB)
cn.row_factory = sqlite3.Row
drivers_db = {d["driver_id"]: dict(d) for d in cn.execute("SELECT * FROM fpoc_drivers")}
print(f"  fpoc_drivers: {len(drivers_db)}")
veh_in_snap = set(int(v["vehicle_id"]) for v in visits)
veh_in_db = set(int(d["vehicle_id"]) for d in drivers_db.values() if d["vehicle_id"] is not None)
extras_snap = veh_in_snap - veh_in_db
extras_db = veh_in_db - veh_in_snap
if extras_snap:
    F("MAJOR", "data", f"vehicles en snapshot sin driver CRUD: {sorted(extras_snap)}")
if extras_db:
    F("MINOR", "data", f"drivers CRUD sin visitas en snapshot: {sorted(extras_db)}")
print(f"  vehicles snap: {sorted(veh_in_snap)}")
print(f"  vehicles CRUD: {sorted(veh_in_db)}")

# 5. Import idempotente
section("5. IMPORT - idempotencia + match")
test_date = "2026-06-15"
cn.execute("DELETE FROM fpoc_planificacion_imports WHERE fecha = ?", (test_date,))
cn.execute("DELETE FROM fpoc_simpli_visits WHERE planned_date = ?", (test_date,))
cn.commit()
r1 = requests.post(f"{BASE}/planificacion/import-mock?fecha={test_date}", headers=H).json()
print(f"  primera carga: count={r1.get('count')} already={r1.get('already_imported')}")
if r1.get("already_imported"):
    F("MAJOR", "import", "primera carga marcada already_imported", r1)
r2 = requests.post(f"{BASE}/planificacion/import-mock?fecha={test_date}", headers=H).json()
print(f"  segunda carga: count={r2.get('count')} already={r2.get('already_imported')}")
if not r2.get("already_imported"):
    F("CRITICAL", "import", "import no idempotente, segunda no marcada", r2)
if r1.get("count") != r2.get("count"):
    F("CRITICAL", "import", f"count distinto re-call: {r1.get('count')} vs {r2.get('count')}")
unmatched = list(cn.execute(
    "SELECT v.driver_name, COUNT(*) FROM fpoc_simpli_visits v "
    "LEFT JOIN fpoc_drivers d ON d.name = v.driver_name "
    "WHERE v.planned_date = ? AND d.driver_id IS NULL GROUP BY v.driver_name",
    (test_date,)
).fetchall())
if unmatched:
    F("MAJOR", "import", f"{len(unmatched)} drivers no matchean fpoc_drivers", [tuple(r) for r in unmatched[:3]])
else:
    print("  100% drivers matchean fpoc_drivers OK")
total = cn.execute("SELECT COUNT(*) FROM fpoc_simpli_visits WHERE planned_date=?", (test_date,)).fetchone()[0]
if total != r1.get("count"):
    F("MAJOR", "import", f"DB count {total} != endpoint {r1.get('count')}")

# 6. Region filters
section("6. FILTROS region")
all_v = len(requests.get(f"{BASE}/visits", headers=H).json())
rm_v = len(requests.get(f"{BASE}/visits?region=RM", headers=H).json())
reg_v = len(requests.get(f"{BASE}/visits?region=regiones", headers=H).json())
print(f"  visits all={all_v} RM={rm_v} reg={reg_v} sum={rm_v+reg_v}")
if rm_v + reg_v != all_v:
    F("MAJOR", "filters", f"region inconsistente: rm+reg={rm_v+reg_v} != all={all_v}")
all_a = len(requests.get(f"{BASE}/alerts/anticipated?limit=200", headers=H).json())
rm_a = len(requests.get(f"{BASE}/alerts/anticipated?limit=200&region=RM", headers=H).json())
reg_a = len(requests.get(f"{BASE}/alerts/anticipated?limit=200&region=regiones", headers=H).json())
print(f"  alerts all={all_a} RM={rm_a} reg={reg_a} sum={rm_a+reg_a}")
if rm_a + reg_a != all_a:
    F("MAJOR", "filters", f"alerts region inconsistente: rm+reg={rm_a+reg_a} != all={all_a}")

# 7. ETA / threshold override
section("7. CONFIG override runtime")
def_alerts = len(requests.get(f"{BASE}/alerts/anticipated?limit=200", headers=H).json())
permissive = len(requests.get(f"{BASE}/alerts/anticipated?limit=200&alert_threshold=0.1", headers=H).json())
restrict = len(requests.get(f"{BASE}/alerts/anticipated?limit=200&eta_window_hours=12", headers=H).json())
print(f"  default={def_alerts} threshold=0.1={permissive} eta=12h={restrict}")
if permissive < def_alerts:
    F("MAJOR", "config", f"threshold=0.1 da {permissive} < default {def_alerts}")
if restrict > def_alerts:
    F("MAJOR", "config", f"eta=12h debería dar <= alerts default")

# 8. WhatsApp flows
section("8. WHATSAPP flows")
sig = requests.get(f"{BASE}/twilio/inbound/test", headers=H).json()
print(f"  validate_signature: {sig.get('validate_signature')}")
SIG_ON = sig.get("validate_signature") == "true"
if SIG_ON:
    print("  signature ON -> mis tests inbound darán 403")


def wa_send(phone, body, profile="QA"):
    r = requests.post(f"{BASE}/twilio/inbound", data={
        "From": f"whatsapp:{phone}", "Body": body,
        "MessageSid": f"SMqa{int(time.time() * 1000) % 100000000}",
        "ProfileName": profile,
    })
    if r.status_code != 200:
        return None, r.status_code
    text = r.text.replace("<?xml version='1.0' encoding='UTF-8'?>", "")
    text = text.replace("<Response>", "").replace("</Response>", "")
    text = text.replace("<Message>", "").replace("</Message>", "")
    return text.strip(), 200


def reset_session(phone):
    cn.execute("DELETE FROM fpoc_whatsapp_sessions WHERE phone_e164=?", (phone,))
    cn.commit()


if not SIG_ON:
    # 8a Driver auto-detect
    PH_DRV = "+56800370025"
    reset_session(PH_DRV)
    resp, st = wa_send(PH_DRV, "hola")
    print(f"  [driver auto-detect] {resp[:80] if resp else f'st={st}'}")
    if not resp or "Jessica" not in (resp or ""):
        F("MAJOR", "wa-driver", f"driver auto-detect falla: {resp[:120] if resp else st}")

    # 8b Manager auto-detect
    PH_MGR = "+56939568904"
    reset_session(PH_MGR)
    resp, st = wa_send(PH_MGR, "hola")
    print(f"  [manager auto-detect] {resp[:80] if resp else f'st={st}'}")
    if not resp or ("Manager" not in (resp or "") and "Jorge" not in (resp or "")):
        F("MAJOR", "wa-mgr", f"manager auto-detect falla: {resp[:120] if resp else st}")

    # 8c Contacto rol jefe
    PH_JEFE = "+56974632487"
    reset_session(PH_JEFE)
    resp, st = wa_send(PH_JEFE, "hola")
    print(f"  [contacto jefe] {resp[:80] if resp else f'st={st}'}")
    if resp and "Manager" not in (resp or ""):
        F("MAJOR", "wa-jefe", f"contacto rol=jefe no entra a menu_manager: {resp[:200]}")

    # 8d Contacto rol driver
    PH_DC = "+56950226229"
    reset_session(PH_DC)
    resp, st = wa_send(PH_DC, "hola")
    print(f"  [contacto driver] {resp[:80] if resp else f'st={st}'}")
    if resp and "FAL-" not in (resp or ""):
        F("MAJOR", "wa-driver-c", f"contacto rol=driver no recibe vehicle auto-asignado: {resp[:200]}")

    # 8e Manuel - flow IA motivo (el que reportó "no toma el motivo")
    MANUEL = "+56951883977"
    reset_session(MANUEL)
    print("\n  [Manuel IA-motivo flow]")
    r1 = wa_send(MANUEL, "hola")[0]
    r2 = wa_send(MANUEL, "3")[0]
    print(f"    hola: {r1[:60] if r1 else None}")
    print(f"    3:    {r2[:60] if r2 else None}")
    v_pend = next((v for v in visits if v["status"] == "pending"), None)
    if v_pend and r2 and "tracking" in (r2 or "").lower():
        tid = v_pend["tracking_id"]
        r3 = wa_send(MANUEL, tid)[0]
        print(f"    {tid}: {r3[:80] if r3 else None}")
        if not r3 or "tus palabras" not in (r3 or "").lower():
            F("CRITICAL", "wa-motivo", f"describing_incident no se invoca: {r3[:120] if r3 else 'EMPTY'}")
        r4 = wa_send(MANUEL, "nadie atendio toque el timbre tres veces")[0]
        print(f"    desc: {r4[:120] if r4 else None}")
        if not r4:
            F("CRITICAL", "wa-motivo", "IA classify no responde")
        elif "Detect" not in (r4 or "") and "motivo" not in (r4 or "").lower():
            F("CRITICAL", "wa-motivo", f"IA no clasifica: {r4[:200]}")
        else:
            r5 = wa_send(MANUEL, "1")[0]
            print(f"    confirm: {r5[:80] if r5 else None}")
            if not r5 or ("Registrado" not in (r5 or "") and "registrado" not in (r5 or "")):
                F("MAJOR", "wa-motivo", f"confirmacion IA no persiste: {r5[:120] if r5 else 'EMPTY'}")
    else:
        F("MAJOR", "wa-motivo", f"flow no avanza a awaiting_tracking; r2={r2[:100] if r2 else None}")

# 9. Reporte
section("9. REPORT FINAL")
sevs = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
total_by_sev = {s: sum(1 for f in findings if f["sev"] == s) for s in sevs}
print(f"\nFindings totales: {len(findings)}")
for s in sevs:
    if total_by_sev[s]:
        print(f"  {s}: {total_by_sev[s]}")

for s in sevs:
    items = [f for f in findings if f["sev"] == s]
    if not items:
        continue
    print(f"\n--- {s} ({len(items)}) ---")
    for f in items:
        print(f"  [{f['area']}] {f['msg']}")
        if f.get("evidence"):
            ev = str(f["evidence"])[:200]
            print(f"     ev: {ev}")

if not any(f["sev"] in ("BLOCKER", "CRITICAL") for f in findings):
    print("\n>>> SIN BLOCKER/CRITICAL — listo para demo <<<")
else:
    print("\n>>> HAY ISSUES BLOCKER/CRITICAL — revisar antes de demo <<<")

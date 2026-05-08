"""QA WhatsApp flows con firma valida (no bypassea validacion).
Necesita TWILIO_AUTH_TOKEN en el env."""
import io
import json
import os
import sys
import time
import sqlite3

# Forzar stdout UTF-8 (Windows cp1252 rompe con emojis del agente).
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator

load_dotenv("../.env")

BASE = "http://localhost:8001/api"
WEBHOOK_URL = "https://flogging-purposely-antonym.ngrok-free.dev/api/twilio/inbound"
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

if not TOKEN:
    print("ERROR: TWILIO_AUTH_TOKEN no esta seteado en env. Sin esto no puedo firmar.")
    sys.exit(1)

VALIDATOR = RequestValidator(TOKEN)
DB = "valuedata.db"
findings = []


def F(sev, area, msg, evidence=None):
    findings.append({"sev": sev, "area": area, "msg": msg, "evidence": evidence})


def wa_send(phone, body, profile="QA"):
    """Envia POST firmado. Llama a ngrok URL para que la firma matchee
    TWILIO_INBOUND_PUBLIC_URL del backend."""
    params = {
        "From": f"whatsapp:{phone}",
        "Body": body,
        "MessageSid": f"SMqa{int(time.time() * 1000) % 100000000}",
        "ProfileName": profile,
        "AccountSid": "ACtest",
        "NumMedia": "0",
        "WaId": phone.lstrip("+"),
    }
    sig = VALIDATOR.compute_signature(WEBHOOK_URL, params)
    r = requests.post(
        WEBHOOK_URL,
        data=params,
        headers={
            "X-Twilio-Signature": sig,
            "ngrok-skip-browser-warning": "1",
        },
        verify=True,
        timeout=15,
    )
    if r.status_code != 200:
        return None, r.status_code, r.text[:200]
    text = r.text
    text = text.replace("<?xml version='1.0' encoding='UTF-8'?>", "")
    text = text.replace("<Response>", "").replace("</Response>", "")
    text = text.replace("<Message>", "").replace("</Message>", "")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.strip(), 200, None


def reset_session(phone):
    cn = sqlite3.connect(DB)
    cn.execute("DELETE FROM fpoc_whatsapp_sessions WHERE phone_e164=?", (phone,))
    cn.commit()
    cn.close()


# Login para hits adicionales
r = requests.post(f"{BASE}/auth/login", json={"email": "admin@falabella.cl", "password": "admin123"})
TOK = r.json()["access_token"]
H = {"Authorization": f"Bearer {TOK}"}

snapshot = requests.get(f"{BASE}/visits", headers=H).json()


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# 1. Driver auto-detect
section("1. Driver auto-detect (+56800370025 Jessica DRV-001)")
PH = "+56800370025"
reset_session(PH)
r, st, err = wa_send(PH, "hola")
print(f"  hola -> {r[:120] if r else f'st={st} err={err}'}")
if not r or "Jessica" not in r:
    F("MAJOR", "driver-detect", f"no auto-detect Jessica: {r[:120] if r else err}")

# 2. Manager auto-detect Jorge
section("2. Manager auto-detect (+56939568904 Jorge)")
PH = "+56939568904"
reset_session(PH)
r, st, err = wa_send(PH, "hola")
print(f"  hola -> {r[:120] if r else f'st={st} err={err}'}")
if not r or "Manager" not in r:
    F("MAJOR", "mgr-detect", f"Jorge no entra a menu_manager: {r[:120] if r else err}")

# 3. Manager Manuel (era manager hasta que lo onboardeamos como driver)
section("3. Manuel detect (+56951883977)")
PH = "+56951883977"
reset_session(PH)
r, st, err = wa_send(PH, "hola")
print(f"  hola -> {r[:120] if r else f'st={st} err={err}'}")
# Manuel está en drivers Y users. Drivers tiene prioridad.
if not r or "Manuel" not in r:
    F("MAJOR", "manuel", f"Manuel no detectado: {r[:120] if r else err}")
elif "Manager" in r:
    F("MAJOR", "manuel", "Manuel detectado como Manager pero está en fpoc_drivers (debería ser driver)")
elif "ruta" not in r.lower() and "veh" not in r.lower():
    F("INFO", "manuel", f"Manuel respuesta: {r[:120]}")

# 4. Contacto rol jefe -> menu_manager
section("4. Contacto rol jefe (+56974632487 anais)")
PH = "+56974632487"
reset_session(PH)
r, st, err = wa_send(PH, "hola")
print(f"  hola -> {r[:120] if r else f'st={st} err={err}'}")
if not r:
    F("MAJOR", "anais", f"sin respuesta: st={st} err={err}")
elif "Manager" not in r:
    F("MAJOR", "anais", f"contacto rol=jefe no entra a menu_manager: {r[:200]}")

# 5. Contacto rol driver -> menu_driver con vehicle auto-asignado
section("5. Contacto rol driver (+56950226229 Belen)")
PH = "+56950226229"
reset_session(PH)
r, st, err = wa_send(PH, "hola")
print(f"  hola -> {r[:200] if r else f'st={st} err={err}'}")
if not r:
    F("MAJOR", "belen", f"sin respuesta: st={st} err={err}")
elif "FAL-" not in r:
    F("MAJOR", "belen", f"contacto rol=driver no recibe vehicle auto-asignado: {r[:200]}")

# 6. Manuel - flow IA motivo end-to-end (el bug "no toma el motivo")
section("6. Flow IA motivo (Manuel)")
PH = "+56951883977"
reset_session(PH)
print(f"  --- step 1: hola ---")
r1, _, _ = wa_send(PH, "hola")
print(f"    {r1[:100] if r1 else None}")
print(f"  --- step 2: 3 (Reportar) ---")
r2, _, _ = wa_send(PH, "3")
print(f"    {r2[:100] if r2 else None}")
if not r2 or "tracking" not in (r2 or "").lower():
    F("CRITICAL", "wa-motivo", f"step 2 falla, no pide tracking: {r2[:120] if r2 else 'EMPTY'}")
else:
    pending = next((v for v in snapshot if v["status"] == "pending"), None)
    if not pending:
        print("  no hay pending visits, salto")
    else:
        tid = pending["tracking_id"]
        print(f"  --- step 3: tracking {tid} ---")
        r3, _, _ = wa_send(PH, tid)
        print(f"    {r3[:200] if r3 else None}")
        if not r3 or "tus palabras" not in (r3 or "").lower():
            F("CRITICAL", "wa-motivo", f"step 3 no pide describir libre: {r3[:200] if r3 else 'EMPTY'}")
        else:
            print(f"  --- step 4: descripcion libre ---")
            r4, _, _ = wa_send(PH, "nadie atendio toque el timbre tres veces y nadie respondio")
            print(f"    {r4[:300] if r4 else None}")
            if not r4:
                F("CRITICAL", "wa-motivo", "step 4 sin respuesta (IA no responde)")
            elif "Detect" not in r4 and "Detec" not in r4:
                F("CRITICAL", "wa-motivo", f"IA no clasifica el motivo: {r4[:300]}")
            else:
                print(f"  --- step 5: confirmar (1) ---")
                r5, _, _ = wa_send(PH, "1")
                print(f"    {r5[:200] if r5 else None}")
                if not r5 or "egistrad" not in (r5 or ""):
                    F("MAJOR", "wa-motivo", f"step 5 confirmacion no persiste: {r5[:200] if r5 else 'EMPTY'}")
                else:
                    print("  IA flow OK end-to-end")

# 7. Flow stop / opt-out
section("7. Compliance: stop")
PH_TEST = "+56977776666"  # Tester
reset_session(PH_TEST)
r1, _, _ = wa_send(PH_TEST, "hola")
r2, _, _ = wa_send(PH_TEST, "stop")
print(f"  hola: {r1[:80] if r1 else None}")
print(f"  stop: {r2[:120] if r2 else None}")
if not r2 or "baja" not in (r2 or "").lower():
    F("MAJOR", "stop", f"stop no responde con confirmación: {r2[:120] if r2 else 'EMPTY'}")

# Reporte
print(f"\n{'=' * 70}\nREPORT WHATSAPP\n{'=' * 70}")
sevs = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
print(f"\nFindings: {len(findings)}")
for s in sevs:
    items = [f for f in findings if f["sev"] == s]
    if items:
        print(f"\n--- {s} ({len(items)}) ---")
        for f in items:
            print(f"  [{f['area']}] {f['msg']}")
            if f.get("evidence"):
                print(f"     {str(f['evidence'])[:200]}")

if not any(f["sev"] in ("BLOCKER", "CRITICAL") for f in findings):
    print("\n>>> WhatsApp flows OK <<<")
else:
    print("\n>>> WhatsApp tiene issues <<<")

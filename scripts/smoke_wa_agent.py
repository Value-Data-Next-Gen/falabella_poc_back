"""Smoke test del agente WhatsApp (twilio_inbound._dispatch).

Ejecutar desde backend/ con .env apuntando al entorno donde quieras probar:

    python -m scripts.smoke_wa_agent

Valida que:
  - Cada comando suelto matchee y devuelva un string no vacío.
  - El FSM (whatsapp_agent.handle) responda en idle con la pregunta de rol.
  - Comandos con argumentos malformados devuelvan mensaje de error legible.

No persiste data (usa phone='smoke:+test' que es bonito de purgar) y al final
borra la sesión que pudo crear.
"""
from __future__ import annotations

import sys
import os

# Asegurar que backend/ esté en path cuando se corre desde scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twilio_inbound import _dispatch  # noqa: E402
from whatsapp_agent import Session  # noqa: E402


PHONE = "smoke:+test"
IDENTITY = {
    "user_id": 0,
    "driver_id": None,
    "empresa_id": None,
    "display_name": "SmokeTest",
    "role": "admin",
    "channel": "test",
}


CASES: list[tuple[str, str, str]] = [
    # (label, mensaje, expected substring [vacío = solo chequear que responda no-vacío])
    ("help",           "help",                    "Comandos"),
    ("info",           "info",                    "Falabella"),
    ("ayuda",          "ayuda",                   "Comandos"),
    ("thanks",         "gracias",                 ""),
    ("kpis",           "kpis",                    ""),
    ("status mal",     "status TID-NO-EXISTE",    ""),
    ("ruta inexist",   "ruta R-99991231-XYZ",     ""),
    ("folio mal",      "folio NO-NUMERIC",        "número"),
    ("folio numérico", "folio 14246780",          ""),  # puede o no existir según data
    ("fsm idle",       "hola",                    ""),  # cae al FSM, debería pedir rol
]


def main() -> int:
    print(f"== smoke_wa_agent · phone={PHONE} ==")
    Session.delete(PHONE)  # baseline limpio

    failed = 0
    for label, body, expect in CASES:
        try:
            reply = _dispatch(body, IDENTITY, PHONE, profile_name="SmokeTest")
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {label!r:18s} -> EXCEPTION: {e}")
            failed += 1
            continue
        ok = reply is not None and reply.strip() != ""
        if expect and (reply or "") and expect.lower() not in (reply or "").lower():
            ok = False
        status = "OK  " if ok else "FAIL"
        snippet = (reply or "").replace("\n", " | ")[:90]
        print(f"[{status}] {label!r:18s} -> {snippet}")
        if not ok:
            failed += 1

    # Cleanup
    try:
        Session.delete(PHONE)
    except Exception:  # noqa: BLE001
        pass

    print(f"\n{len(CASES) - failed}/{len(CASES)} casos OK")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

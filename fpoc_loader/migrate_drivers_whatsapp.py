"""Sprint 4.A1 — Drivers WhatsApp opt-in fields + notifications_log.driver_id.

Agrega a `fpoc_drivers`:
  - phone_e164         TEXT  (normalizado E.164, NULL si no parsea)
  - notify_whatsapp    INTEGER NOT NULL DEFAULT 0
  - opted_in_at        TIMESTAMP

Agrega a `fpoc_notifications_log`:
  - driver_id          TEXT

Para cada driver con `phone` no vacío intenta normalizar a E.164 y poblar
`phone_e164`. Si no parsea queda NULL. Bajo NINGUNA circunstancia activa
notify_whatsapp ni setea opted_in_at — eso queda para el admin manual.

Idempotente: ejecutarlo dos veces no rompe.

Uso:
    python valuedata_backend/fpoc_loader/migrate_drivers_whatsapp.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv
for _p in (BACKEND / ".env", BACKEND.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from core.db import backend, get_conn  # noqa: E402


_DIGITS_RE = re.compile(r"\D+")


def _parse_to_e164(phone_text: str | None) -> str | None:
    """Normaliza '+56 9 1234 5678' / '+56912345678' / '56912345678' → '+56912345678'.

    Rules:
      - Strip everything except digits and the leading '+'.
      - If the phone starts with '+' → keep as-is after digit cleanup.
      - If the phone starts with '56' and has 11 digits → assume CL → prefix '+'.
      - If the phone starts with '9' and has 9 digits → assume CL mobile → '+56' prefix.
      - Otherwise → None (unrecognized format).
    """
    if not phone_text:
        return None
    raw = str(phone_text).strip()
    if not raw:
        return None

    has_plus = raw.startswith("+")
    digits = _DIGITS_RE.sub("", raw)
    if not digits:
        return None

    if has_plus:
        # Trust the country prefix as provided
        if len(digits) < 8 or len(digits) > 15:
            return None
        return f"+{digits}"

    # Heuristics for Chilean numbers (most likely scenario in this POC)
    if digits.startswith("56") and 11 <= len(digits) <= 12:
        return f"+{digits}"
    if digits.startswith("9") and len(digits) == 9:
        return f"+56{digits}"
    if len(digits) == 8:  # móvil CL antiguo sin el 9 inicial
        return f"+569{digits}"

    return None


def _column_exists(cn, table: str, column: str) -> bool:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"PRAGMA table_info({table})")
        rows = cur.fetchall()
        return any(r[1].lower() == column.lower() for r in rows)
    cur.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? AND COLUMN_NAME = ?",
        table.replace("fpoc_", ""), column,
    )
    return cur.fetchone() is not None


def _add_column(cn, table: str, column: str, ddl: str) -> bool:
    if _column_exists(cn, table, column):
        print(f"[skip] {table}.{column} ya existe")
        return False
    cur = cn.cursor()
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    cn.commit()
    print(f"[ok]   {table}.{column} agregado")
    return True


def _backfill_phone_e164(cn) -> tuple[int, int, int]:
    """Pobla phone_e164 a partir de phone para todas las filas que tienen
    phone_e164 NULL. Devuelve (total, parsed, unparsed)."""
    cur = cn.cursor()
    cur.execute(
        "SELECT driver_id, phone FROM fpoc_drivers WHERE phone_e164 IS NULL AND phone IS NOT NULL"
    )
    rows = cur.fetchall()
    parsed = 0
    unparsed = 0
    for r in rows:
        e164 = _parse_to_e164(r.phone)
        if e164:
            cur.execute(
                "UPDATE fpoc_drivers SET phone_e164 = ? WHERE driver_id = ?",
                e164, r.driver_id,
            )
            parsed += 1
        else:
            unparsed += 1
    cn.commit()
    return len(rows), parsed, unparsed


def main() -> int:
    print(f"[migrate] backend={backend()}")
    with get_conn() as cn:
        # 1) Drivers: 3 columnas nuevas
        _add_column(cn, "fpoc_drivers", "phone_e164",
                    "phone_e164 TEXT")
        _add_column(cn, "fpoc_drivers", "notify_whatsapp",
                    "notify_whatsapp INTEGER NOT NULL DEFAULT 0")
        _add_column(cn, "fpoc_drivers", "opted_in_at",
                    "opted_in_at TIMESTAMP")

        # 2) Notifications_log: driver_id
        _add_column(cn, "fpoc_notifications_log", "driver_id",
                    "driver_id TEXT")

        # 3) Backfill phone_e164 desde phone (no toca notify_whatsapp ni opted_in_at)
        total, parsed, unparsed = _backfill_phone_e164(cn)
        print(f"[backfill] phone_e164: {parsed}/{total} parseados ({unparsed} sin formato reconocido)")

        # 4) Resumen
        cur = cn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM fpoc_drivers WHERE notify_whatsapp = 1 AND opted_in_at IS NOT NULL"
        )
        n_optin = int(cur.fetchone().n)
        print(f"[summary] drivers con notify_whatsapp=1 + opted_in_at: {n_optin} (esperado 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

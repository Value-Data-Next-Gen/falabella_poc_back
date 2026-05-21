"""Sprint 4.A1 — Drivers WhatsApp opt-in fields + notifications_log.driver_id.

Estado: las tablas/columnas ya están en Azure SQL aplicadas a mano en sprints
anteriores. Esta migración queda como no-op en el registry
(`019_drivers_whatsapp` → `_noop`). El módulo se mantiene por compat de import.

Para una nueva instancia de Azure SQL desde cero, aplicar manualmente:
  ALTER TABLE fpoc.drivers ADD phone_e164 NVARCHAR(20) NULL;
  ALTER TABLE fpoc.drivers ADD notify_whatsapp BIT NOT NULL DEFAULT 0;
  ALTER TABLE fpoc.drivers ADD opted_in_at DATETIME2(0) NULL;
  ALTER TABLE fpoc.notifications_log ADD driver_id NVARCHAR(20) NULL;
"""
from __future__ import annotations

import re
import sys


_DIGITS_RE = re.compile(r"\D+")


def _parse_to_e164(phone_text: str | None) -> str | None:
    """Normaliza '+56 9 1234 5678' / '+56912345678' / '56912345678' → '+56912345678'.

    Helper público — el normalizador real vive en otros módulos pero acá queda
    por compat con tests/scripts que lo importen de este path.
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
        if len(digits) < 8 or len(digits) > 15:
            return None
        return f"+{digits}"

    if digits.startswith("56") and 11 <= len(digits) <= 12:
        return f"+{digits}"
    if digits.startswith("9") and len(digits) == 9:
        return f"+56{digits}"
    if len(digits) == 8:
        return f"+569{digits}"

    return None


def main() -> int:
    """No-op. Mantener símbolo por compat con registry y scripts antiguos."""
    print("[migrate-drivers-whatsapp] no-op (Azure SQL único backend; tablas ya aplicadas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

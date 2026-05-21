"""Migración: agrega columnas deadline a fpoc.vip_clients.

Estado: las columnas ya están en Azure SQL aplicadas a mano en sprints
previos. Esta migración queda como no-op en el registry
(`022_vip_deadline` → `_noop`). El módulo se mantiene por compat de import.

Para una nueva instancia de Azure SQL desde cero, aplicar manualmente:
  ALTER TABLE fpoc.vip_clients ADD deadline_time NVARCHAR(8) NULL;
  ALTER TABLE fpoc.vip_clients ADD alert_minutes_before INT NOT NULL
      CONSTRAINT DF_vip_alert_min DEFAULT 60 WITH VALUES;
  ALTER TABLE fpoc.vip_clients ADD last_alert_sent_at DATETIME2 NULL;
"""
from __future__ import annotations

import sys


def main() -> int:
    """No-op. Mantener símbolo por compat con registry y scripts antiguos."""
    print("[migrate-vip-deadline] no-op (Azure SQL único backend; columnas ya aplicadas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

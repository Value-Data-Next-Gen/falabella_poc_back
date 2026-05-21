"""Sprint 4.A2 — Tabla `fpoc.motivo_corrections`.

Estado: la tabla ya está en Azure SQL aplicada a mano en sprints previos.
Esta migración queda como no-op en el registry
(`021_motivo_corrections` → `_noop`). El módulo se mantiene por compat de
import.

Para una nueva instancia de Azure SQL desde cero, aplicar manualmente el DDL
correspondiente (ver `_legacy/bootstrap_azure_schema.py`).
"""
from __future__ import annotations

import sys


def main() -> int:
    """No-op. Mantener símbolo por compat con registry y scripts antiguos."""
    print("[migrate-motivo-corrections] no-op (Azure SQL único backend; ya aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

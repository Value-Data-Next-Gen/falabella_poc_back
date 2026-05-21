"""Bootstrap inicial (Azure SQL only).

Históricamente este módulo bootstrappeaba la DB SQLite local: aplicaba schema,
seedeaba users de demo, y cargaba `datos_eta_*.xlsx`. Con el backend ahora
acotado a Azure SQL como único motor, el schema se administra:

  - DDL inicial: `fpoc_loader/ddl.sql` (aplicado a mano una vez por entorno)
  - Migraciones incrementales: `fpoc_loader/migrate_*.py` registradas en
    `migrations.MIGRATIONS` y aplicadas idempotentes en cada arranque

Quedamos exponiendo `bootstrap_if_needed()` por compat con `migrations.py`
(`001_bootstrap_if_needed` en el registry) — es no-op contra Azure SQL.
"""
from __future__ import annotations

from loguru import logger


def bootstrap_if_needed(*, quiet: bool = False) -> None:
    """No-op en Azure SQL. Existía para SQLite local; se mantiene el símbolo
    para no romper el registry de migraciones (001_bootstrap_if_needed)."""
    if not quiet:
        logger.debug("[bootstrap] no-op (Azure SQL único backend; schema admin via migraciones)")


__all__ = ["bootstrap_if_needed"]

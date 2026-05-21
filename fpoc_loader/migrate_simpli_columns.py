"""Migración 026: agrega region, comuna, ruta_id a fpoc.simpli_visits si faltan.

Histórico: estas columnas se agregaban en `_legacy/fpoc_loader/bootstrap_azure_schema.py`
(líneas 247-249) pero quedaba huérfano para nuevos deploys Azure. Esta
migración lo formaliza y la incorpora al registry idempotente.

En SQLite la tabla `fpoc_simpli_visits` ya se crea con esas columnas en
`sqlite_schema.sql`, así que esta migración es no-op en ese backend.

Idempotente: chequea INFORMATION_SCHEMA antes de cada ALTER.

Uso manual:
    python -m fpoc_loader.migrate_simpli_columns   # requiere DB_BACKEND=sqlserver

Corre automáticamente en lifespan via fpoc_loader.migrations.MIGRATIONS bajo
el id "026_simpli_columns".
"""
from __future__ import annotations

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

from loguru import logger  # noqa: E402

from core.db import backend as db_backend, get_conn  # noqa: E402


COLUMNS: tuple[tuple[str, str], ...] = (
    ("region",  "ALTER TABLE fpoc.simpli_visits ADD region NVARCHAR(50) NULL"),
    ("comuna",  "ALTER TABLE fpoc.simpli_visits ADD comuna NVARCHAR(100) NULL"),
    ("ruta_id", "ALTER TABLE fpoc.simpli_visits ADD ruta_id NVARCHAR(50) NULL"),
)


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def main(quiet: bool = False) -> None:
    """Aplica las columnas faltantes en fpoc.simpli_visits (Azure SQL únicamente)."""
    if db_backend() != "sqlserver":
        _log("[migrate-simpli-columns] backend!=sqlserver, no-op", quiet)
        return

    _log("[migrate-simpli-columns] backend=sqlserver", quiet)
    with get_conn() as cn:
        cur = cn.cursor()
        for col, ddl in COLUMNS:
            try:
                cur.execute(
                    f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE Name = N'{col}'
                          AND Object_ID = Object_ID(N'fpoc.simpli_visits')
                    )
                    BEGIN
                        {ddl}
                    END
                    """
                )
                cn.commit()
                _log(f"[ok]   fpoc.simpli_visits.{col} asegurada", quiet)
            except Exception as e:  # noqa: BLE001
                # Si falla por permisos o porque la tabla no existe aún,
                # loggear pero no romper boot.
                logger.warning(f"[migrate-simpli-columns] {col}: {e}")


if __name__ == "__main__":
    main()

"""Migración 029: agrega columna `updated_at` a fpoc.drivers.

Los handlers PUT /api/admin/drivers/{id} y PUT /api/mantenedores/drivers/{id}
hacen `SET updated_at = CURRENT_TIMESTAMP` que fallaba con 500 porque la
columna no existía.

Es idempotente (chequea sys.columns).
"""
from __future__ import annotations

from loguru import logger

from core.db import get_conn


MIGRATION_ID = "029_drivers_updated_at"


def run() -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.columns "
            "  WHERE object_id=OBJECT_ID('fpoc.drivers') AND name='updated_at') "
            "ALTER TABLE fpoc.drivers "
            "  ADD updated_at DATETIME2 NOT NULL "
            "  CONSTRAINT DF_drivers_updated_at DEFAULT SYSDATETIME()"
        )
        cn.commit()
    logger.info(f"[{MIGRATION_ID}] OK — fpoc.drivers.updated_at agregado")
    return {"migration": MIGRATION_ID, "status": "ok"}


if __name__ == "__main__":
    run()

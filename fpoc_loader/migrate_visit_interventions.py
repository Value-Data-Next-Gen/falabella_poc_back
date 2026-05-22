"""Migración 028: tabla fpoc.visit_interventions (audit log de admin actions).

Cuando un admin Falabella interviene un folio (cancel/reschedule/escalate/
override_motivo), guardamos el evento aquí. Permite auditoría retrospectiva
y reportes "qué tan seguido Falabella interviene flotas X".

También agrega columnas opcionales a fpoc.simpli_visits:
  - priority    VARCHAR(10) NULL  -- 'HIGH' cuando admin la marca como urgente
  - cancelled_at DATETIME2  NULL  -- timestamp si fue cancelada

Es idempotente (chequea sys.tables / sys.columns).
"""
from __future__ import annotations

from loguru import logger

from core.db import get_conn


MIGRATION_ID = "028_visit_interventions"


def run() -> dict:
    with get_conn() as cn:
        cur = cn.cursor()

        # 1) tabla visit_interventions
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.tables t "
            "  JOIN sys.schemas s ON s.schema_id=t.schema_id "
            "  WHERE s.name='fpoc' AND t.name='visit_interventions') "
            "CREATE TABLE fpoc.visit_interventions ("
            "  intervention_id BIGINT IDENTITY(1,1) PRIMARY KEY,"
            "  tracking_id     NVARCHAR(64) NOT NULL,"
            "  action          NVARCHAR(40) NOT NULL,"
            "  admin_user_id   INT NULL,"
            "  admin_name      NVARCHAR(200) NULL,"
            "  before_value    NVARCHAR(MAX) NULL,"
            "  after_value     NVARCHAR(MAX) NULL,"
            "  reason          NVARCHAR(500) NULL,"
            "  created_at      DATETIME2 NOT NULL CONSTRAINT DF_visit_interv_created DEFAULT SYSDATETIME()"
            ")"
        )

        # 2) índice por tracking_id
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_visit_interv_tid') "
            "CREATE INDEX IX_visit_interv_tid ON fpoc.visit_interventions(tracking_id)"
        )

        # 3) priority en simpli_visits
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.columns "
            "  WHERE object_id=OBJECT_ID('fpoc.simpli_visits') AND name='priority') "
            "ALTER TABLE fpoc.simpli_visits ADD priority NVARCHAR(10) NULL"
        )

        # 4) cancelled_at en simpli_visits
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.columns "
            "  WHERE object_id=OBJECT_ID('fpoc.simpli_visits') AND name='cancelled_at') "
            "ALTER TABLE fpoc.simpli_visits ADD cancelled_at DATETIME2 NULL"
        )

        cn.commit()

    logger.info(f"[{MIGRATION_ID}] OK — tabla visit_interventions + columnas priority/cancelled_at")
    return {"migration": MIGRATION_ID, "status": "ok"}


if __name__ == "__main__":
    run()

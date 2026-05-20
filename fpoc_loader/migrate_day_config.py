"""Crea fpoc.day_config — configuración específica por día operativo.

Cada fila representa overrides para un día específico:
  - cutoff_time: hora límite del día (ej. 19:00). Visitas pendientes después
    se marcan auto como failed por un cron (segunda etapa).
  - message_to_drivers: texto libre que el WhatsApp agent inyecta al saludo
    de cualquier driver que se conecte ese día.
  - alert_threshold_override / slack_min_override: overrides puntuales de
    fpoc.app_config para ese día.
  - restricted_vehicles / restricted_empresas: JSON list de ids que NO
    operan ese día.

Idempotente.
"""
from __future__ import annotations

from loguru import logger

from core.db import backend as db_backend, get_conn


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[day-config] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            IF OBJECT_ID('fpoc.day_config', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.day_config (
                    fecha                       DATE          NOT NULL PRIMARY KEY,
                    cutoff_time                 TIME          NULL,
                    message_to_drivers          NVARCHAR(MAX) NULL,
                    alert_threshold_override    DECIMAL(4,3)  NULL,
                    slack_min_override          INT           NULL,
                    restricted_vehicle_ids      NVARCHAR(MAX) NULL,
                    restricted_empresa_ids      NVARCHAR(MAX) NULL,
                    set_by_user_id              INT           NULL,
                    created_at                  DATETIME2(0)  NOT NULL CONSTRAINT DF_dayconf_created DEFAULT SYSDATETIME(),
                    updated_at                  DATETIME2(0)  NULL
                );
            END
            """
        )
        cn.commit()
        if not quiet:
            logger.info("[day-config] tabla lista")


if __name__ == "__main__":
    main()

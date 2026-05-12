"""Crea fpoc.driver_positions para la simulación de movimiento de drivers
en tiempo real (Ronda 4 / driver sim).

Cada fila representa el snapshot más reciente de un driver:
  - vehicle_id, patente_falsa
  - fecha (planned_date que está EN_CURSO)
  - ruta_id, current_stop_order (último stop completado o entregando)
  - next_stop_order (próximo a hacer)
  - lat, lon (interpolados entre stops)
  - ts_sim (sim_clock al que corresponde la posición)
  - status: 'en_ruta' | 'entregando' | 'detenido' | 'finalizado'
  - speed_kmh (estimada para mostrar en UI)

NO histórico — un row por driver. La simulación UPDATE in-place.
"""
from __future__ import annotations

from loguru import logger

from db import backend as db_backend, get_conn


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[driver-positions] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            IF OBJECT_ID('fpoc.driver_positions', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.driver_positions (
                    vehicle_id       INT             NOT NULL,
                    planned_date     DATE            NOT NULL,
                    ruta_id          NVARCHAR(50)    NULL,
                    driver_name      NVARCHAR(120)   NULL,
                    patente_falsa    INT             NULL,
                    current_stop     INT             NULL,
                    next_stop        INT             NULL,
                    lat              FLOAT           NULL,
                    lon              FLOAT           NULL,
                    ts_sim           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
                    status           NVARCHAR(20)    NOT NULL DEFAULT 'en_ruta',
                    speed_kmh        FLOAT           NULL,
                    updated_at       DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
                    CONSTRAINT PK_driver_positions PRIMARY KEY (vehicle_id, planned_date)
                );
                CREATE INDEX IX_dp_fecha ON fpoc.driver_positions(planned_date);
                CREATE INDEX IX_dp_ruta ON fpoc.driver_positions(ruta_id);
            END
            """
        )
        cn.commit()
        if not quiet:
            logger.info("[driver-positions] tabla lista")


if __name__ == "__main__":
    main()

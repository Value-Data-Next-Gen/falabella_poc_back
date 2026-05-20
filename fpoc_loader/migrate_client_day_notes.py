"""Crea fpoc.client_day_notes — notas y marcas VIP por (fecha, cliente).

Permite tener anotaciones específicas del día sobre un cliente sin tocar
fpoc.vip_clients (que es el registry permanente). Si vip_marked_here=1
significa que ese día se decidió marcarlo como VIP (info redundante con
vip_clients pero útil para auditoría de qué se decidió en cada jornada).

Idempotente. No-op en SQLite (esquema espejo se crea solo si DB_BACKEND=sqlite).
"""
from __future__ import annotations

from loguru import logger

from core.db import backend as db_backend, get_conn


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[client-day-notes] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            IF OBJECT_ID('fpoc.client_day_notes', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.client_day_notes (
                    id              INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    fecha           DATE              NOT NULL,
                    cliente         NVARCHAR(200)     NOT NULL,
                    notes           NVARCHAR(MAX)     NULL,
                    vip_marked_here BIT               NOT NULL CONSTRAINT DF_cdn_vip_marked DEFAULT 0,
                    set_by_user_id  INT               NULL,
                    created_at      DATETIME2(0)      NOT NULL CONSTRAINT DF_cdn_created DEFAULT SYSDATETIME(),
                    updated_at      DATETIME2(0)      NULL,
                    CONSTRAINT UQ_cdn_fecha_cliente UNIQUE (fecha, cliente)
                );
                CREATE INDEX IX_cdn_fecha ON fpoc.client_day_notes(fecha);
            END
            """
        )
        cn.commit()
        if not quiet:
            logger.info("[client-day-notes] tabla lista")


if __name__ == "__main__":
    main()

"""Extiende fpoc.planificacion_imports con la máquina de estados del día.

Estados: BORRADOR → LISTO → EN_CURSO ↔ PAUSADO → CERRADO

Columnas nuevas:
  state           NVARCHAR(20) NOT NULL DEFAULT 'BORRADOR'
  paused_at       DATETIME2(0) NULL
  closed_at       DATETIME2(0) NULL
  day_seed        INT NULL   — seed del generator usado al iniciar

started_at + started_by_user_id ya se agregaron en migración previa.

Reglas:
  - Estado por default = BORRADOR si la fila ya existe sin state (legacy).
  - Filas con started_at IS NOT NULL → se backfillean a EN_CURSO.
"""
from __future__ import annotations

from loguru import logger

from db import backend as db_backend, get_conn


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[day-state] backend no-mssql, skip")
        return
    sqls = [
        ("state",         "ALTER TABLE fpoc.planificacion_imports ADD state NVARCHAR(20) NOT NULL CONSTRAINT DF_planimp_state DEFAULT 'BORRADOR'"),
        ("paused_at",     "ALTER TABLE fpoc.planificacion_imports ADD paused_at DATETIME2(0) NULL"),
        ("closed_at",     "ALTER TABLE fpoc.planificacion_imports ADD closed_at DATETIME2(0) NULL"),
        ("day_seed",      "ALTER TABLE fpoc.planificacion_imports ADD day_seed INT NULL"),
    ]
    with get_conn() as cn:
        cur = cn.cursor()
        for col, sql in sqls:
            cur.execute(
                "SELECT COL_LENGTH('fpoc.planificacion_imports', ?)",
                col,
            )
            if cur.fetchone()[0] is None:
                cur.execute(sql)
                cn.commit()
                if not quiet:
                    logger.info(f"[day-state] + {col}")
        # Backfill: filas con started_at no nulo → state=EN_CURSO si quedó BORRADOR.
        cur.execute(
            "UPDATE fpoc.planificacion_imports "
            "SET state = 'EN_CURSO' "
            "WHERE started_at IS NOT NULL AND state = 'BORRADOR'"
        )
        n = cur.rowcount or 0
        cn.commit()
        # CHECK constraint sobre los 5 valores válidos
        cur.execute(
            "SELECT 1 FROM sys.check_constraints WHERE name = 'CK_planimp_state'"
        )
        if not cur.fetchone():
            cur.execute(
                "ALTER TABLE fpoc.planificacion_imports "
                "ADD CONSTRAINT CK_planimp_state "
                "CHECK (state IN ('BORRADOR','LISTO','EN_CURSO','PAUSADO','CERRADO'))"
            )
            cn.commit()
            if not quiet:
                logger.info("[day-state] CHECK constraint CK_planimp_state agregado")
        if not quiet:
            logger.info(f"[day-state] backfill EN_CURSO: {n} filas")


if __name__ == "__main__":
    main()

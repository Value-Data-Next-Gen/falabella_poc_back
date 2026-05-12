"""Ronda 3: rename LISTO→VALIDADO + drop PAUSADO en day-state machine.

Cambios:
  1. UPDATE filas con state='LISTO' → 'VALIDADO'.
  2. UPDATE filas con state='PAUSADO' → 'EN_CURSO' (PAUSADO se elimina;
     volvemos al estado activo, el live_gen igual está apagado salvo
     toggle explícito).
  3. DROP CHECK constraint vieja CK_planimp_state.
  4. ADD CHECK constraint nueva con 4 valores: BORRADOR / VALIDADO / EN_CURSO / CERRADO.

Idempotente.
"""
from __future__ import annotations

from loguru import logger

from db import backend as db_backend, get_conn


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[day-state-r3] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()

        # 1) Backfill rows con estados viejos
        cur.execute("UPDATE fpoc.planificacion_imports SET state = 'VALIDADO' WHERE state = 'LISTO'")
        n_listo = cur.rowcount or 0
        cur.execute("UPDATE fpoc.planificacion_imports SET state = 'EN_CURSO' WHERE state = 'PAUSADO'")
        n_pausado = cur.rowcount or 0
        cn.commit()

        # 2) DROP old CHECK + ADD new
        cur.execute("SELECT 1 FROM sys.check_constraints WHERE name = 'CK_planimp_state'")
        if cur.fetchone():
            cur.execute("ALTER TABLE fpoc.planificacion_imports DROP CONSTRAINT CK_planimp_state")
            cn.commit()
            if not quiet:
                logger.info("[day-state-r3] DROP CHECK CK_planimp_state (vieja)")
        cur.execute(
            "ALTER TABLE fpoc.planificacion_imports "
            "ADD CONSTRAINT CK_planimp_state "
            "CHECK (state IN ('BORRADOR','VALIDADO','EN_CURSO','CERRADO'))"
        )
        cn.commit()
        if not quiet:
            logger.info(
                f"[day-state-r3] LISTO→VALIDADO: {n_listo} · PAUSADO→EN_CURSO: {n_pausado} · "
                f"CHECK nueva activa"
            )


if __name__ == "__main__":
    main()

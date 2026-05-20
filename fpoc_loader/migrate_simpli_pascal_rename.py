"""Renombra las 5 columnas PascalCase de fpoc.simpli_visits a snake_case.

Heredadas del export XLSX de SimpliRoute. El importador hace el mapeo
antes de INSERT así que el XLSX sigue funcionando igual.

Idempotente: chequea si la columna vieja existe antes de renombrar.
No-op en SQLite (sp_rename es T-SQL).
"""
from __future__ import annotations

from loguru import logger

from core.db import backend as db_backend, get_conn

# Nombres construidos por concatenación para que un futuro grep/sed global
# no nos los toque por accidente.
_E = "E" + "mpresa_falsa"
_P = "P" + "atente_falsa"
_D = "D" + "rivername"
_F1 = "F" + "echainicioruta"
_F2 = _F1 + "_hora_cl"

RENAMES: list[tuple[str, str]] = [
    (_E,  "empresa_falsa"),
    (_P,  "patente_falsa"),
    (_D,  "driver_name"),
    (_F2, "fecha_inicio_ruta_hora_cl"),   # más específica primero
    (_F1, "fecha_inicio_ruta"),
]


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[simpli-rename] backend no-mssql, skip")
        return
    renamed = 0
    skipped = 0
    with get_conn() as cn:
        cur = cn.cursor()
        for old, new in RENAMES:
            # ¿Existe la vieja o la nueva?
            cur.execute(
                "SELECT COL_LENGTH('fpoc.simpli_visits', ?) AS old_len, "
                "COL_LENGTH('fpoc.simpli_visits', ?) AS new_len",
                old, new,
            )
            r = cur.fetchone()
            if r.old_len is None and r.new_len is not None:
                skipped += 1
                continue
            if r.old_len is None and r.new_len is None:
                logger.warning(f"[simpli-rename] ninguna columna '{old}' ni '{new}' existe")
                continue
            cur.execute(
                "EXEC sp_rename ?, ?, 'COLUMN'",
                f"fpoc.simpli_visits.{old}", new,
            )
            cn.commit()
            renamed += 1
            if not quiet:
                logger.info(f"[simpli-rename] {old} -> {new}")
    if not quiet:
        logger.info(f"[simpli-rename] renamed={renamed} skipped={skipped}")


if __name__ == "__main__":
    main()

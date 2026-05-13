"""Limpia legacy rol='driver' en fpoc.empresa_contactos y agrega CHECK constraint.

Background: hasta ahora `rol` no tenía CHECK a nivel DB y el backend aceptaba
los 5 valores ('jefe','coordinador','dispatcher','driver','otro'). La UI desde
hace tiempo no permite crear 'driver' (los drivers van en fpoc.drivers).
Quedaron filas legacy. Esta migración:

  1. UPDATE rol='driver' → rol='otro' (mantiene la fila para no perder phone/email).
  2. ALTER TABLE ADD CONSTRAINT CK_empresa_contactos_rol que limita a los 4 válidos.

Idempotente: chequea por nombre de la constraint antes de agregar.
No-op en SQLite.
"""
from __future__ import annotations

from loguru import logger

from core.db import backend as db_backend, get_conn


CONSTRAINT_NAME = "CK_empresa_contactos_rol"
ALLOWED = ("jefe", "coordinador", "dispatcher", "otro")


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[empcontactos-rol] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()

        # 1) Migrar filas legacy
        legacy_marker = "d" + "river"  # sed-safe
        cur.execute(
            "UPDATE fpoc.empresa_contactos SET rol = 'otro' WHERE rol = ?",
            legacy_marker,
        )
        migrated = cur.rowcount or 0
        cn.commit()

        # 2) ¿Existe ya la CHECK constraint?
        cur.execute(
            "SELECT 1 FROM sys.check_constraints WHERE name = ?",
            CONSTRAINT_NAME,
        )
        if cur.fetchone():
            if not quiet:
                logger.info(f"[empcontactos-rol] CHECK ya existe, migrated={migrated}")
            return

        # 3) Validar que no queden valores fuera de la lista (defensivo)
        cur.execute(
            "SELECT DISTINCT rol FROM fpoc.empresa_contactos "
            "WHERE rol NOT IN ('jefe','coordinador','dispatcher','otro')"
        )
        invalid = [r.rol for r in cur.fetchall()]
        if invalid:
            logger.warning(
                f"[empcontactos-rol] valores inválidos restantes: {invalid}. "
                "Limpiá antes de agregar el CHECK."
            )
            return

        cur.execute(
            f"ALTER TABLE fpoc.empresa_contactos "
            f"ADD CONSTRAINT {CONSTRAINT_NAME} "
            f"CHECK (rol IN ('jefe','coordinador','dispatcher','otro'))"
        )
        cn.commit()
        if not quiet:
            logger.info(f"[empcontactos-rol] migrated={migrated} CHECK agregado")


if __name__ == "__main__":
    main()

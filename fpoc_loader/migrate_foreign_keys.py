"""Agrega FOREIGN KEYs faltantes en Azure SQL.

Las validamos primero (0 huérfanos en todas las relaciones al momento de
introducir la migración). Cada FK se crea con un nombre explícito y se
chequea contra sys.foreign_keys para que la migración sea idempotente.

ON DELETE SET NULL en columnas que apuntan a fpoc.users (las acciones del
usuario no deben caer si se borra al usuario). NO ACTION en el resto —
borrar una empresa o un driver con dependencias debería fallar a propósito.

No-op en SQLite (las FK se declaran inline en sqlite_schema.sql).
"""
from __future__ import annotations

from loguru import logger

from core.db import backend as db_backend, get_conn


# (constraint_name, child_table, child_col, parent_table, parent_col, on_delete)
FKS: list[tuple[str, str, str, str, str, str]] = [
    ("FK_drivers_vehicle",        "fpoc.drivers",                  "vehicle_id",           "fpoc.vehicles",            "vehicle_id", "NO ACTION"),
    ("FK_drivers_empresa",        "fpoc.drivers",                  "empresa_id",           "fpoc.empresas_transporte", "empresa_id", "NO ACTION"),
    ("FK_vehicles_empresa",       "fpoc.vehicles",                 "empresa_id",           "fpoc.empresas_transporte", "empresa_id", "NO ACTION"),
    # FK_visit_comments_visit excluida: tracking_id es NVARCHAR(50) pero
    # simpli_visits.id es BIGINT — incompatible para FK declarada. La
    # integridad se mantiene a nivel aplicación.
    ("FK_empcontactos_empresa",   "fpoc.empresa_contactos",        "empresa_id",           "fpoc.empresas_transporte", "empresa_id", "NO ACTION"),
    ("FK_empcontactos_user",      "fpoc.empresa_contactos",        "created_by_user_id",   "fpoc.users",               "user_id",    "SET NULL"),
    ("FK_vip_empresa",            "fpoc.vip_clients",              "empresa_id",           "fpoc.empresas_transporte", "empresa_id", "NO ACTION"),
    ("FK_vip_creator",            "fpoc.vip_clients",              "created_by",           "fpoc.users",               "user_id",    "SET NULL"),
    ("FK_prio_user",              "fpoc.visit_priority_overrides", "set_by",               "fpoc.users",               "user_id",    "SET NULL"),
    ("FK_notif_user",             "fpoc.notifications_log",        "user_id",              "fpoc.users",               "user_id",    "SET NULL"),
    ("FK_dotacion_empresa",       "fpoc.dotacion_diaria",          "empresa_id",           "fpoc.empresas_transporte", "empresa_id", "NO ACTION"),
    ("FK_dotacion_driver",        "fpoc.dotacion_diaria",          "driver_id",            "fpoc.drivers",             "driver_id",  "NO ACTION"),
    ("FK_dotacion_vehicle",       "fpoc.dotacion_diaria",          "vehicle_id",           "fpoc.vehicles",            "vehicle_id", "NO ACTION"),
    ("FK_planimport_user",        "fpoc.planificacion_imports",    "imported_by_user_id",  "fpoc.users",               "user_id",    "SET NULL"),
    # NO ACTION acá porque FK_planimport_user (imported_by_user_id) ya
    # tiene SET NULL — SQL Server no permite múltiples cascade paths
    # hacia la misma tabla destino.
    ("FK_planimport_starter",     "fpoc.planificacion_imports",    "started_by_user_id",   "fpoc.users",               "user_id",    "NO ACTION"),
    ("FK_users_empresa",          "fpoc.users",                    "empresa_id",           "fpoc.empresas_transporte", "empresa_id", "NO ACTION"),
]


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[migrate-fk] backend no-mssql, skip")
        return
    added = 0
    skipped = 0
    failed = 0
    with get_conn() as cn:
        cur = cn.cursor()
        for name, ct, cc, pt, pc, on_delete in FKS:
            try:
                # Existe ya?
                cur.execute(
                    "SELECT 1 FROM sys.foreign_keys WHERE name = ?",
                    name,
                )
                if cur.fetchone():
                    skipped += 1
                    continue
                # Validar 0 huérfanos antes de agregar (defensive: el dataset
                # puede haber cambiado entre la validación inicial y la corrida).
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {ct} c "
                    f"LEFT JOIN {pt} p ON c.{cc} = p.{pc} "
                    f"WHERE c.{cc} IS NOT NULL AND p.{pc} IS NULL"
                )
                n_orphans = int(cur.fetchone().n or 0)
                if n_orphans > 0:
                    logger.warning(
                        f"[migrate-fk] {name} skip: {n_orphans} huérfanos en "
                        f"{ct}.{cc} → {pt}.{pc}. Limpiá antes de agregar la FK."
                    )
                    skipped += 1
                    continue
                cur.execute(
                    f"ALTER TABLE {ct} "
                    f"ADD CONSTRAINT {name} FOREIGN KEY ({cc}) "
                    f"REFERENCES {pt}({pc}) ON DELETE {on_delete}"
                )
                cn.commit()
                added += 1
                if not quiet:
                    logger.info(f"[migrate-fk] + {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.warning(f"[migrate-fk] {name} falló: {str(e)[:200]}")
    if not quiet:
        logger.info(f"[migrate-fk] added={added} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()

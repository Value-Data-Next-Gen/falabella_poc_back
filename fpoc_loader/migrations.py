"""Registry de migraciones idempotentes con tracking en `fpoc.schema_migrations`.

Usage en main.lifespan:

    from fpoc_loader.migrations import apply_all, MIGRATIONS

    apply_all(MIGRATIONS)

Cada migración se marca como aplicada DESPUÉS de correr sin error. Si falla,
no se marca y se reintenta en el próximo arranque. Las migraciones existentes
ya son idempotentes (IF NOT EXISTS / IF COL_LENGTH), así que aunque corran 2
veces no rompen nada — solo evitamos el costo de reejecutarlas.
"""
from __future__ import annotations

from typing import Callable
from loguru import logger

from db import backend as db_backend, get_conn


def _ensure_migrations_table() -> None:
    """Idempotente en SQLite + Azure SQL."""
    with get_conn() as cn:
        cur = cn.cursor()
        if db_backend() == "sqlserver":
            cur.execute(
                """
                IF OBJECT_ID('fpoc.schema_migrations', 'U') IS NULL
                BEGIN
                    CREATE TABLE fpoc.schema_migrations (
                        migration_id NVARCHAR(200) NOT NULL PRIMARY KEY,
                        applied_at DATETIME2(0) NOT NULL DEFAULT SYSDATETIME()
                    )
                END
                """
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fpoc_schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        cn.commit()


def _is_applied(migration_id: str) -> bool:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT 1 FROM fpoc_schema_migrations WHERE migration_id = ?",
            migration_id,
        )
        return cur.fetchone() is not None


def _mark_applied(migration_id: str) -> None:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "INSERT INTO fpoc_schema_migrations (migration_id) VALUES (?)",
            migration_id,
        )
        cn.commit()


def apply_migration(migration_id: str, fn: Callable[[], None]) -> bool:
    """Corre `fn` solo si `migration_id` no está en el registry.

    Devuelve True si se ejecutó, False si ya estaba aplicada o falló.
    """
    try:
        _ensure_migrations_table()
        if _is_applied(migration_id):
            logger.debug(f"[migration] skip {migration_id} (ya aplicada)")
            return False
        logger.info(f"[migration] aplicando {migration_id}…")
        fn()
        _mark_applied(migration_id)
        logger.info(f"[migration] ok {migration_id}")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[migration] {migration_id} falló (se reintentará en próximo arranque): {e}")
        return False


def apply_all(migrations: list[tuple[str, Callable[[], None]]]) -> None:
    """Aplica una lista [(id, fn), …] en orden, deteniéndose en el primer fallo
    duro NO — registramos y seguimos para no bloquear el arranque por una
    migración legacy."""
    _ensure_migrations_table()
    for mid, fn in migrations:
        apply_migration(mid, fn)


# ---------- Lista canónica de migraciones (orden importa) ----------
def _wrap_quiet(import_path: str, func_name: str = "main") -> Callable[[], None]:
    """Importa lazy y llama `func_name(quiet=True)` para mantener compat con los
    scripts existentes que aceptan ese kwarg."""
    def _run() -> None:
        mod = __import__(import_path, fromlist=[func_name])
        fn = getattr(mod, func_name)
        try:
            fn(quiet=True)
        except TypeError:
            fn()
    return _run


MIGRATIONS: list[tuple[str, Callable[[], None]]] = [
    ("001_bootstrap_if_needed",       _wrap_quiet("fpoc_loader.bootstrap", "bootstrap_if_needed")),
    ("002_dotacion_diaria",           _wrap_quiet("fpoc_loader.migrate_dotacion_diaria")),
    ("003_driver_documents",          _wrap_quiet("fpoc_loader.migrate_driver_documents")),
    ("004_capacitaciones",            _wrap_quiet("fpoc_loader.migrate_capacitaciones")),
    ("005_driver_role",               _wrap_quiet("fpoc_loader.migrate_driver_role")),
    ("006_empresa_central",           _wrap_quiet("fpoc_loader.migrate_empresa_central")),
    ("007_document_types",            _wrap_quiet("fpoc_loader.migrate_document_types")),
    ("008_entity_documents",          _wrap_quiet("fpoc_loader.migrate_entity_documents")),
    ("009_cap_validation",            _wrap_quiet("fpoc_loader.migrate_cap_validation")),
    ("010_foreign_keys",              _wrap_quiet("fpoc_loader.migrate_foreign_keys")),
    ("011_simpli_pascal_rename",      _wrap_quiet("fpoc_loader.migrate_simpli_pascal_rename")),
    ("012_empcontactos_rol_check",    _wrap_quiet("fpoc_loader.migrate_empresa_contactos_rol_check")),
    ("013_client_day_notes",          _wrap_quiet("fpoc_loader.migrate_client_day_notes")),
    ("014_day_config",                _wrap_quiet("fpoc_loader.migrate_day_config")),
]

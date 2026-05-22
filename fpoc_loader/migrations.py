"""Registry de migraciones idempotentes con tracking en `fpoc.schema_migrations`.

Usage en main.lifespan:

    from fpoc_loader.migrations import apply_all, MIGRATIONS

    apply_all(MIGRATIONS)

Cada migración se marca como aplicada DESPUÉS de correr sin error. Si falla,
no se marca y se reintenta en el próximo arranque. Las migraciones existentes
ya son idempotentes (IF NOT EXISTS / IF COL_LENGTH), así que aunque corran 2
veces no rompen nada — solo evitamos el costo de reejecutarlas.

Backend único: Azure SQL. El runner solo conoce sintaxis MSSQL.
"""
from __future__ import annotations

from typing import Callable
from loguru import logger

from core.db import get_conn


def _ensure_migrations_table() -> None:
    """Crea fpoc.schema_migrations si no existe. Idempotente."""
    with get_conn() as cn:
        cur = cn.cursor()
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
    """Aplica una lista [(id, fn), …] en orden. No corta en fallos duros:
    registramos y seguimos para no bloquear el arranque por una migración
    legacy."""
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


def _noop(_label: str) -> Callable[[], None]:
    """Stub para migraciones que históricamente eran sqlite-only. Las tablas
    correspondientes ya están en Azure SQL (aplicadas a mano en sprints
    anteriores); las dejamos registradas como aplicadas para preservar la
    secuencia numérica del registry."""
    def _run() -> None:
        logger.debug(f"[migration] {_label}: no-op (sqlite-only legacy, ya aplicada a mano en Azure)")
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
    ("015_day_state_machine",         _wrap_quiet("fpoc_loader.migrate_day_state")),
    ("016_day_state_r3",              _wrap_quiet("fpoc_loader.migrate_day_state_r3")),
    ("017_split_multi_region_routes", _wrap_quiet("fpoc_loader.migrate_split_multi_region_routes")),
    ("018_driver_positions",          _wrap_quiet("fpoc_loader.migrate_driver_positions")),
    # 019..022: históricamente escritas en SQLite puro (CREATE IF NOT EXISTS,
    # AUTOINCREMENT). En Azure SQL las tablas ya existen aplicadas a mano —
    # las dejamos como no-op para preservar el registry numerado y evitar
    # 'Incorrect syntax near IF' en cada arranque.
    ("019_drivers_whatsapp",          _noop("019_drivers_whatsapp")),
    ("020_empresa_contactos_table",   _noop("020_empresa_contactos_table")),
    ("021_motivo_corrections",        _noop("021_motivo_corrections")),
    ("022_vip_deadline",              _noop("022_vip_deadline")),
    # CR-012 T0.3: alert_dispatch_log + supervisor_phone_e164 en empresas.
    ("023_alert_dispatch_phones",     _wrap_quiet("fpoc_loader.migrate_alert_dispatch")),
    # CR-013: copiloto_decisions — feedback del operador sobre sugerencias IA.
    ("024_copiloto_decisions",        _wrap_quiet("fpoc_loader.migrate_copiloto_decisions")),
    # CR-014: activation_token + activation_used_at en users/drivers/contactos.
    # Habilita wa.me activation links (workaround del error 63112 de Meta).
    ("025_activation_tokens",         _wrap_quiet("fpoc_loader.migrate_activation_tokens")),
    # Validator fix #1: region/comuna/ruta_id en fpoc.simpli_visits.
    ("026_simpli_columns",            _wrap_quiet("fpoc_loader.migrate_simpli_columns")),
    # Fase 3 MVP: piloto controlable. Agrega sim_clock_offset_min y lat/lon.
    ("027_sim_clock",                 _wrap_quiet("fpoc_loader.migrate_sim_clock")),
    # Pieza #6: admin intervention sobre folios + audit table.
    ("028_visit_interventions",       _wrap_quiet("fpoc_loader.migrate_visit_interventions")),
    # QA fix: PUT /api/admin/drivers fallaba 500 por columna ausente.
    ("029_drivers_updated_at",        _wrap_quiet("fpoc_loader.migrate_drivers_updated_at")),
]

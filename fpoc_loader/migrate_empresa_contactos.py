"""Migración: separa "destinatarios WhatsApp" de "usuarios login".

Crea la tabla `fpoc_empresa_contactos` (si no existe), agrega `contact_id` a
`fpoc_notifications_log` (si no existe), y migra los users actuales que en
realidad son contactos transportistas (no usuarios login):

- jorge@valuedata.cl (uid=15, empresa=22) -> rol 'jefe'
- manuel@valuedata.cl (uid=17, empresa=22) -> rol 'driver'

Ambos quedan con `opted_in_at=NULL` (no han hecho join al sandbox WhatsApp). En
`fpoc_users` se desactivan (`activo=0`) para que no aparezcan en login.

Idempotente: ejecutarlo dos veces no duplica filas ni rompe nada.

Uso:
    python valuedata_backend/fpoc_loader/migrate_empresa_contactos.py

Respeta DB_BACKEND (usa el wrapper `db.get_conn()`), por lo que funciona tanto
con SQLite POC como con SQL Server (Azure).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permite ejecutar este script directo sin instalar el paquete: añade backend/
# al sys.path para que `import db` resuelva.
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv

# Cargar .env antes de importar db (lee DB_BACKEND, SQLITE_PATH, etc.)
for _p in (BACKEND / ".env", BACKEND.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from db import backend, get_conn  # noqa: E402


# Migrate-list: (email, rol_destino)
USERS_TO_MIGRATE: list[tuple[str, str]] = [
    ("jorge@valuedata.cl", "jefe"),
    ("manuel@valuedata.cl", "driver"),
]


DDL_CONTACTOS = """
CREATE TABLE IF NOT EXISTS fpoc_empresa_contactos (
    contact_id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    empresa_id          INTEGER  NOT NULL,
    nombre              TEXT     NOT NULL,
    rol                 TEXT     NOT NULL CHECK (rol IN ('jefe','coordinador','dispatcher','driver','otro')),
    phone_e164          TEXT     NOT NULL,
    email               TEXT,
    severities_in       TEXT,
    motivos_in          TEXT,
    region_filter       TEXT     NOT NULL DEFAULT 'all' CHECK (region_filter IN ('RM','regiones','all')),
    opted_in_at         TIMESTAMP,
    active              INTEGER  NOT NULL DEFAULT 1,
    notes               TEXT,
    created_by_user_id  INTEGER,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id),
    FOREIGN KEY (created_by_user_id) REFERENCES fpoc_users(user_id)
);
"""

DDL_INDEX_EMPRESA = (
    "CREATE INDEX IF NOT EXISTS idx_fpoc_empresa_contactos_empresa "
    "ON fpoc_empresa_contactos(empresa_id, active);"
)
DDL_INDEX_PHONE = (
    "CREATE INDEX IF NOT EXISTS idx_fpoc_empresa_contactos_phone "
    "ON fpoc_empresa_contactos(phone_e164);"
)


def _column_exists(cn, table: str, column: str) -> bool:
    """SQLite-only: usa PRAGMA table_info. Para SQL Server hay que adaptar."""
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(f"PRAGMA table_info({table})")
        rows = cur.fetchall()
        return any(r[1].lower() == column.lower() for r in rows)
    # SQL Server
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = ? AND COLUMN_NAME = ?
        """,
        table.replace("fpoc_", ""), column,
    )
    return cur.fetchone() is not None


def _ensure_table_and_indexes(cn) -> None:
    cur = cn.cursor()
    cur.execute(DDL_CONTACTOS)
    cur.execute(DDL_INDEX_EMPRESA)
    cur.execute(DDL_INDEX_PHONE)
    cn.commit()
    print("[ddl] fpoc_empresa_contactos OK")


def _ensure_contact_id_in_notifications_log(cn) -> None:
    """ALTER TABLE para agregar `contact_id` si no existe."""
    if _column_exists(cn, "fpoc_notifications_log", "contact_id"):
        print("[ddl] fpoc_notifications_log.contact_id ya existe")
        return
    cur = cn.cursor()
    cur.execute("ALTER TABLE fpoc_notifications_log ADD COLUMN contact_id INTEGER")
    cn.commit()
    print("[ddl] fpoc_notifications_log.contact_id agregado")


def _find_user(cn, email: str) -> dict | None:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT user_id, email, display_name, empresa_id, phone_e164, activo
        FROM fpoc_users WHERE email = ?
        """,
        email,
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "user_id": int(r.user_id),
        "email": r.email,
        "display_name": r.display_name,
        "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
        "phone_e164": r.phone_e164,
        "activo": bool(r.activo),
    }


def _contact_exists(cn, empresa_id: int, phone: str) -> bool:
    cur = cn.cursor()
    cur.execute(
        """
        SELECT 1 FROM fpoc_empresa_contactos
        WHERE empresa_id = ? AND phone_e164 = ?
        """,
        empresa_id, phone,
    )
    return cur.fetchone() is not None


def _migrate_user(cn, email: str, rol: str) -> str:
    user = _find_user(cn, email)
    if not user:
        return f"[skip] {email}: no existe en fpoc_users"
    if user["empresa_id"] is None:
        return f"[skip] {email}: sin empresa_id, no se puede migrar"
    if not user["phone_e164"]:
        return f"[skip] {email}: sin phone_e164"

    cur = cn.cursor()
    if not _contact_exists(cn, user["empresa_id"], user["phone_e164"]):
        cur.execute(
            """
            INSERT INTO fpoc_empresa_contactos
              (empresa_id, nombre, rol, phone_e164, email, region_filter,
               opted_in_at, active, notes, created_by_user_id)
            VALUES (?, ?, ?, ?, ?, 'all', NULL, 1, ?, NULL)
            """,
            user["empresa_id"], user["display_name"], rol,
            user["phone_e164"], user["email"],
            f"Migrado desde fpoc_users uid={user['user_id']} ({email})",
        )
        msg_insert = "insertado"
    else:
        msg_insert = "ya existía"

    # Desactiva el user en fpoc_users si todavía está activo (idempotente).
    if user["activo"]:
        cur.execute("UPDATE fpoc_users SET activo = 0 WHERE user_id = ?", user["user_id"])
        msg_user = "fpoc_users.activo=0"
    else:
        msg_user = "fpoc_users ya inactivo"

    cn.commit()
    return f"[ok] {email} (uid={user['user_id']}, empresa={user['empresa_id']}): contacto {msg_insert}, {msg_user}"


def main() -> int:
    print(f"[migrate] backend={backend()}")
    with get_conn() as cn:
        _ensure_table_and_indexes(cn)
        _ensure_contact_id_in_notifications_log(cn)
        for email, rol in USERS_TO_MIGRATE:
            print(_migrate_user(cn, email, rol))

        # Resumen
        cur = cn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM fpoc_empresa_contactos")
        n_contactos = int(cur.fetchone().n)
        cur.execute(
            "SELECT COUNT(*) AS n FROM fpoc_empresa_contactos WHERE opted_in_at IS NOT NULL"
        )
        n_optin = int(cur.fetchone().n)
        print(f"[summary] fpoc_empresa_contactos: {n_contactos} filas, {n_optin} con opt-in")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Seed de empresas_transporte + users para POC.

Toma las empresa_falsa distintas que hay en fpoc.simpli_visits y las carga como
empresas. Crea 1 admin Falabella + 1 transport_manager por empresa.

Seguridad: el password del admin se lee de `INITIAL_ADMIN_PASSWORD` (env var).
Si la env var no está seteada, se cae a `admin123` con WARNING — útil para
dev local pero **rotar inmediatamente en producción**. Los demos passwords
(ops, transport_manager) siguen siendo fijos porque son ambientes de POC sin
datos sensibles; rotarlos si el POC pasa a producción.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pyodbc
from dotenv import load_dotenv
from loguru import logger
from passlib.hash import bcrypt

HERE = Path(__file__).resolve().parent
DDL_PATH = HERE / "users_ddl.sql"


def _resolve_admin_password() -> str:
    """Lee `INITIAL_ADMIN_PASSWORD` o cae a 'admin123' con warning.

    Lazy para que la env var pueda venir de `.env` cargado por get_conn().
    """
    pwd = os.environ.get("INITIAL_ADMIN_PASSWORD")
    if not pwd:
        logger.warning(
            "⚠️  Usando password default 'admin123' para admin@falabella.cl — "
            "setear INITIAL_ADMIN_PASSWORD en .env para producción."
        )
        return "admin123"
    return pwd


ADMIN = {
    "email": "admin@falabella.cl",
    # Resuelto en main() (cuando ya cargó .env).
    "password": None,
    "display_name": "Admin Falabella",
    "role": "falabella_admin",
    "empresa_id": None,
}
OPS = {
    "email": "ops@falabella.cl",
    "password": "ops123",
    "display_name": "Operaciones Falabella",
    "role": "falabella_ops",
    "empresa_id": None,
}
DEFAULT_TRANSPORT_PASSWORD = "demo123"


def get_conn() -> pyodbc.Connection:
    # Busca .env en backend/ y en project root (un nivel arriba).
    for p in (HERE.parent / ".env", HERE.parent.parent / ".env"):
        if p.exists():
            load_dotenv(p)
            break
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    conn_str = (
        f"DRIVER={{{os.environ['DB_DRIVER']}}};"
        f"SERVER={server};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, autocommit=False)


def apply_ddl(cn: pyodbc.Connection) -> None:
    sql = DDL_PATH.read_text(encoding="utf-8")
    batches = [b.strip() for b in sql.split("\nGO\n") if b.strip()]
    cur = cn.cursor()
    for b in batches:
        cur.execute(b)
    cn.commit()
    print(f"[ddl] OK ({len(batches)} batches)")


def seed_empresas(cn: pyodbc.Connection) -> list[int]:
    cur = cn.cursor()
    cur.execute("SELECT DISTINCT empresa_falsa FROM fpoc.simpli_visits ORDER BY empresa_falsa")
    ids = [int(r[0]) for r in cur.fetchall()]
    for eid in ids:
        nombre = f"Transporte {eid:02d}"
        cur.execute(
            """
            MERGE fpoc.empresas_transporte AS t
            USING (SELECT ? AS empresa_id, ? AS nombre) AS s
              ON t.empresa_id = s.empresa_id
            WHEN NOT MATCHED THEN INSERT (empresa_id, nombre) VALUES (s.empresa_id, s.nombre);
            """,
            eid, nombre,
        )
    cn.commit()
    print(f"[empresas] seed {len(ids)} empresas (IDs: {ids[:5]}{'...' if len(ids) > 5 else ''})")
    return ids


def upsert_user(cn: pyodbc.Connection, *, email: str, password: str, display_name: str,
                role: str, empresa_id: int | None) -> None:
    pwd_hash = bcrypt.hash(password)
    cur = cn.cursor()
    cur.execute(
        """
        MERGE fpoc.users AS t
        USING (SELECT ? AS email) AS s ON t.email = s.email
        WHEN MATCHED THEN UPDATE SET
            password_hash = ?, display_name = ?, role = ?, empresa_id = ?, activo = 1
        WHEN NOT MATCHED THEN INSERT (email, password_hash, display_name, role, empresa_id)
            VALUES (?, ?, ?, ?, ?);
        """,
        email,
        pwd_hash, display_name, role, empresa_id,
        email, pwd_hash, display_name, role, empresa_id,
    )
    cn.commit()


def main() -> int:
    with get_conn() as cn:
        apply_ddl(cn)
        empresa_ids = seed_empresas(cn)

        # Resolver password admin DESPUÉS de get_conn() para que .env esté
        # cargado por load_dotenv() en get_conn().
        ADMIN["password"] = _resolve_admin_password()
        upsert_user(cn, **ADMIN)
        upsert_user(cn, **OPS)
        # No imprimir el password real para evitar PII en logs/CI.
        print(f"[users] admin: {ADMIN['email']} / <password set>")
        print(f"[users] ops:   {OPS['email']} / {OPS['password']}")

        for eid in empresa_ids:
            upsert_user(
                cn,
                email=f"transporte{eid:02d}@demo.cl",
                password=DEFAULT_TRANSPORT_PASSWORD,
                display_name=f"Manager Transporte {eid:02d}",
                role="transport_manager",
                empresa_id=eid,
            )
        print(f"[users] transport_manager: {len(empresa_ids)} usuarios, todos con password '{DEFAULT_TRANSPORT_PASSWORD}'")

        cur = cn.cursor()
        cur.execute("SELECT role, COUNT(*) FROM fpoc.users GROUP BY role")
        for r in cur.fetchall():
            print(f"  total {r[0]}: {r[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

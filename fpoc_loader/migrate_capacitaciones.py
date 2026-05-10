"""Capacitaciones de drivers.

Dos tablas:
- capacitacion_modulos: catálogo (codigo, nombre, validez_meses, descripcion, activo).
- driver_capacitaciones: hechos (driver_id, modulo_id, fecha_completado, vence_at,
  notas, archivo opcional). vence_at se calcula al insertar/actualizar como
  fecha_completado + validez_meses (pero también se puede setear manual).

Idempotente sqlite/sqlserver. Seedea un catálogo inicial si la tabla está vacía.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv

for _p in (BACKEND / ".env", BACKEND.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from db import backend, get_conn  # noqa: E402


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


SEED_MODULOS = [
    ("MANEJO_DEFENSIVO", "Manejo defensivo",                  12, "Curso anual obligatorio."),
    ("CARGA_PELIGROSA",  "Manejo de carga peligrosa",         24, "Validez 2 años."),
    ("PRIMEROS_AUXILIOS","Primeros auxilios",                 24, ""),
    ("MANIPULACION",     "Manipulación de productos",         12, ""),
    ("PROTOCOLO_FAL",    "Protocolo Falabella",               12, "Onboarding inicial + refresh anual."),
    ("PREVENCION_RIESGO","Prevención de riesgos laborales",   24, ""),
]


def _ensure_tables(cn, quiet: bool) -> None:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fpoc_capacitacion_modulos (
                modulo_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo         TEXT     NOT NULL UNIQUE,
                nombre         TEXT     NOT NULL,
                descripcion    TEXT,
                validez_meses  INTEGER  NOT NULL DEFAULT 12,
                activo         INTEGER  NOT NULL DEFAULT 1,
                created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fpoc_driver_capacitaciones (
                cap_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id        TEXT     NOT NULL,
                modulo_id        INTEGER  NOT NULL,
                fecha_completado DATE     NOT NULL,
                vence_at         DATE,
                notas            TEXT,
                doc_id           INTEGER,
                created_by       INTEGER,
                created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (driver_id) REFERENCES fpoc_drivers(driver_id),
                FOREIGN KEY (modulo_id) REFERENCES fpoc_capacitacion_modulos(modulo_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS IX_driver_caps_driver
            ON fpoc_driver_capacitaciones(driver_id, modulo_id)
            """
        )
    else:
        cur.execute(
            """
            IF OBJECT_ID('fpoc.capacitacion_modulos', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.capacitacion_modulos (
                    modulo_id     INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    codigo        NVARCHAR(50)  NOT NULL UNIQUE,
                    nombre        NVARCHAR(200) NOT NULL,
                    descripcion   NVARCHAR(500) NULL,
                    validez_meses INT           NOT NULL DEFAULT 12,
                    activo        BIT           NOT NULL DEFAULT 1,
                    created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
                );
            END
            """
        )
        cur.execute(
            """
            IF OBJECT_ID('fpoc.driver_capacitaciones', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.driver_capacitaciones (
                    cap_id           INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    driver_id        NVARCHAR(20)  NOT NULL,
                    modulo_id        INT           NOT NULL,
                    fecha_completado DATE          NOT NULL,
                    vence_at         DATE          NULL,
                    notas            NVARCHAR(500) NULL,
                    doc_id           INT           NULL,
                    created_by       INT           NULL,
                    created_at       DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
                );
            END
            """
        )
        cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_driver_caps_driver')
                CREATE INDEX IX_driver_caps_driver
                ON fpoc.driver_capacitaciones(driver_id, modulo_id)
            """
        )
    cn.commit()
    _log("[ok]   capacitaciones tablas", quiet)


def _seed_catalog(cn, quiet: bool) -> None:
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc.capacitacion_modulos")
    n = int(cur.fetchone()[0])
    if n > 0:
        _log(f"[skip] catalogo ya tiene {n} módulos", quiet)
        return
    for codigo, nombre, validez, desc in SEED_MODULOS:
        cur.execute(
            "INSERT INTO fpoc.capacitacion_modulos (codigo, nombre, validez_meses, descripcion) "
            "VALUES (?, ?, ?, ?)",
            codigo, nombre, validez, desc,
        )
    cn.commit()
    _log(f"[ok]   catalogo seedeado ({len(SEED_MODULOS)} módulos)", quiet)


def main(quiet: bool = False) -> None:
    _log(f"[migrate-capacitaciones] backend={backend()}", quiet)
    with get_conn() as cn:
        _ensure_tables(cn, quiet)
        _seed_catalog(cn, quiet)


if __name__ == "__main__":
    main()

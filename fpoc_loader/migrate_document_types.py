"""Catálogo configurable de tipos de documentos por entidad.

entity_type ∈ ('driver', 'vehicle', 'empresa').
Cada tipo declara si es obligatorio + meses de validez sugeridos.

Idempotente sqlite/sqlserver. Seed inicial si la tabla está vacía.
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

from core.db import backend, get_conn  # noqa: E402


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


# (entity_type, codigo, nombre, mandatory, validez_meses)
SEED = [
    # Driver
    ("driver",  "licencia",         "Licencia de conducir",          True,  60),
    ("driver",  "antecedentes",     "Antecedentes",                  True,  6),
    ("driver",  "contrato",         "Contrato laboral",              True,  None),
    ("driver",  "poliza",           "Póliza de seguro",              False, 12),
    ("driver",  "certificacion",    "Certificación adicional",       False, 24),
    # Vehicle
    ("vehicle", "revision_tecnica", "Revisión técnica",              True,  12),
    ("vehicle", "permiso_circ",     "Permiso de circulación",        True,  12),
    ("vehicle", "soap",             "SOAP",                          True,  12),
    ("vehicle", "poliza",           "Póliza de seguro",              False, 12),
    ("vehicle", "padron",           "Padrón",                        True,  None),
    # Empresa
    ("empresa", "contrato_fal",     "Contrato Falabella",            True,  None),
    ("empresa", "rut",              "RUT empresa",                   True,  None),
    ("empresa", "poliza_rc",        "Póliza responsabilidad civil",  True,  12),
    ("empresa", "cert_vigencia",    "Certificado vigencia sociedad", False, 6),
]


def _ensure_table(cn, quiet: bool) -> None:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fpoc_document_types (
                doc_type_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type      TEXT     NOT NULL CHECK (entity_type IN ('driver','vehicle','empresa')),
                codigo           TEXT     NOT NULL,
                nombre           TEXT     NOT NULL,
                mandatory        INTEGER  NOT NULL DEFAULT 0,
                validez_meses    INTEGER,
                active           INTEGER  NOT NULL DEFAULT 1,
                created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_type, codigo)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS IX_doc_types_entity ON fpoc_document_types(entity_type, active)"
        )
    else:
        cur.execute(
            """
            IF OBJECT_ID('fpoc.document_types', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.document_types (
                    doc_type_id    INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    entity_type    NVARCHAR(20)  NOT NULL,
                    codigo         NVARCHAR(50)  NOT NULL,
                    nombre         NVARCHAR(200) NOT NULL,
                    mandatory      BIT           NOT NULL DEFAULT 0,
                    validez_meses  INT           NULL,
                    active         BIT           NOT NULL DEFAULT 1,
                    created_at     DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
                    CONSTRAINT CK_doc_types_entity CHECK (entity_type IN ('driver','vehicle','empresa')),
                    CONSTRAINT UQ_doc_types_entity_codigo UNIQUE (entity_type, codigo)
                );
            END
            """
        )
        cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_doc_types_entity')
                CREATE INDEX IX_doc_types_entity ON fpoc.document_types(entity_type, active)
            """
        )
    cn.commit()
    _log("[ok]   document_types", quiet)


def _seed(cn, quiet: bool) -> None:
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM fpoc.document_types")
    n = int(cur.fetchone()[0])
    if n > 0:
        _log(f"[skip] document_types ya tiene {n} filas", quiet)
        return
    for entity, codigo, nombre, mand, validez in SEED:
        cur.execute(
            "INSERT INTO fpoc.document_types (entity_type, codigo, nombre, mandatory, validez_meses) "
            "VALUES (?, ?, ?, ?, ?)",
            entity, codigo, nombre, 1 if mand else 0, validez,
        )
    cn.commit()
    _log(f"[ok]   seed document_types ({len(SEED)} tipos)", quiet)


def main(quiet: bool = False) -> None:
    _log(f"[migrate-document-types] backend={backend()}", quiet)
    with get_conn() as cn:
        _ensure_table(cn, quiet)
        _seed(cn, quiet)


if __name__ == "__main__":
    main()

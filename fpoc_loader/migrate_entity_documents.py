"""empresa_documents y vehicle_documents — mismo modelo que driver_documents.

Tablas paralelas para no romper el código que ya hace JOIN/scope contra
driver_documents. Si crece, se puede unificar en una sola tabla con
entity_type/entity_id en el futuro.
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


def _ensure(cn, quiet: bool) -> None:
    cur = cn.cursor()
    if backend() == "sqlite":
        for tbl, fk_col, fk_type, fk_tbl in [
            ("fpoc_empresa_documents", "empresa_id", "INTEGER", "fpoc_empresas_transporte"),
            ("fpoc_vehicle_documents", "vehicle_id", "INTEGER", "fpoc_vehicles"),
        ]:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {tbl} (
                    doc_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    {fk_col}            {fk_type} NOT NULL,
                    tipo                TEXT     NOT NULL,
                    filename            TEXT     NOT NULL,
                    blob_path           TEXT     NOT NULL,
                    file_size           INTEGER  NOT NULL DEFAULT 0,
                    content_type        TEXT,
                    uploaded_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    uploaded_by_user_id INTEGER,
                    expires_at          DATE,
                    notes               TEXT,
                    FOREIGN KEY ({fk_col}) REFERENCES {fk_tbl}({fk_col})
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS IX_{tbl}_scope ON {tbl}({fk_col}, tipo)"
            )
    else:
        for schema_tbl, fk_col, fk_type in [
            ("empresa_documents", "empresa_id", "INT"),
            ("vehicle_documents", "vehicle_id", "INT"),
        ]:
            cur.execute(
                f"""
                IF OBJECT_ID('fpoc.{schema_tbl}', 'U') IS NULL
                BEGIN
                    CREATE TABLE fpoc.{schema_tbl} (
                        doc_id              INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                        {fk_col}            {fk_type}     NOT NULL,
                        tipo                NVARCHAR(50)  NOT NULL,
                        filename            NVARCHAR(255) NOT NULL,
                        blob_path           NVARCHAR(500) NOT NULL,
                        file_size           INT           NOT NULL DEFAULT 0,
                        content_type        NVARCHAR(100) NULL,
                        uploaded_at         DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
                        uploaded_by_user_id INT           NULL,
                        expires_at          DATE          NULL,
                        notes               NVARCHAR(500) NULL
                    );
                END
                """
            )
            cur.execute(
                f"""
                IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_{schema_tbl}_scope')
                    CREATE INDEX IX_{schema_tbl}_scope ON fpoc.{schema_tbl}({fk_col}, tipo)
                """
            )
    cn.commit()
    _log("[ok]   empresa_documents + vehicle_documents", quiet)


def main(quiet: bool = False) -> None:
    _log(f"[migrate-entity-documents] backend={backend()}", quiet)
    with get_conn() as cn:
        _ensure(cn, quiet)


if __name__ == "__main__":
    main()

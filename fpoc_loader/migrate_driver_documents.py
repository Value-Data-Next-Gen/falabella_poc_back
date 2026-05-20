"""Driver documents: tabla para almacenar metadata de documentos subidos.

El archivo binario vive en Azure Blob Storage (o filesystem local en dev);
la DB solo guarda metadatos + la ruta del blob.

Idempotente sqlite/sqlserver, mismo patrón que migrate_dotacion_diaria.
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


def _ensure_table(cn, quiet: bool) -> None:
    cur = cn.cursor()
    if backend() == "sqlite":
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fpoc_driver_documents (
                doc_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id           TEXT     NOT NULL,
                tipo                TEXT     NOT NULL,
                filename            TEXT     NOT NULL,
                blob_path           TEXT     NOT NULL,
                file_size           INTEGER  NOT NULL DEFAULT 0,
                content_type        TEXT,
                uploaded_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                uploaded_by_user_id INTEGER,
                expires_at          DATE,
                notes               TEXT,
                FOREIGN KEY (driver_id) REFERENCES fpoc_drivers(driver_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS IX_driver_docs_driver_tipo
            ON fpoc_driver_documents(driver_id, tipo)
            """
        )
    else:
        cur.execute(
            """
            IF OBJECT_ID('fpoc.driver_documents', 'U') IS NULL
            BEGIN
                CREATE TABLE fpoc.driver_documents (
                    doc_id              INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    driver_id           NVARCHAR(20)  NOT NULL,
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
            """
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_driver_docs_driver_tipo')
                CREATE INDEX IX_driver_docs_driver_tipo
                ON fpoc.driver_documents(driver_id, tipo)
            """
        )
    cn.commit()
    _log("[ok]   driver_documents", quiet)


def main(quiet: bool = False) -> None:
    _log(f"[migrate-driver-documents] backend={backend()}", quiet)
    with get_conn() as cn:
        _ensure_table(cn, quiet)


if __name__ == "__main__":
    main()

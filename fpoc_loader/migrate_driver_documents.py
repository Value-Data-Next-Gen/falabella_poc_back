"""Driver documents: tabla para almacenar metadata de documentos subidos.

El archivo binario vive en Azure Blob Storage (o filesystem local en dev);
la DB solo guarda metadatos + la ruta del blob.

Idempotente. Azure SQL como único backend.
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

from core.db import get_conn  # noqa: E402


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _ensure_table(cn, quiet: bool) -> None:
    cur = cn.cursor()
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
    _log("[migrate-driver-documents] backend=sqlserver", quiet)
    with get_conn() as cn:
        _ensure_table(cn, quiet)


if __name__ == "__main__":
    main()

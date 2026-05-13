"""Sprint 4.A2 — Tabla `fpoc_motivo_corrections`.

Crea la tabla y los índices que necesita el flujo de validación LLM automática
del motivo reportado por el chofer.

Idempotente.

Uso:
    python valuedata_backend/fpoc_loader/migrate_motivo_corrections.py
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


DDL_TABLE = """
CREATE TABLE IF NOT EXISTS fpoc_motivo_corrections (
    correction_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id         INTEGER NOT NULL,
    tracking_id        TEXT NOT NULL,
    motivo_reportado   TEXT NOT NULL,
    motivo_sugerido    TEXT NOT NULL,
    confianza          TEXT NOT NULL,
    razonamiento       TEXT NOT NULL,
    driver_id          TEXT,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','accepted','rejected','no_action')),
    decided_by_user_id INTEGER,
    decided_at         TIMESTAMP,
    notified_driver_at TIMESTAMP,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (comment_id) REFERENCES fpoc_visit_comments(comment_id)
);
"""

DDL_INDEX_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_corrections_status "
    "ON fpoc_motivo_corrections(status, created_at);"
)
DDL_INDEX_DRIVER = (
    "CREATE INDEX IF NOT EXISTS idx_corrections_driver "
    "ON fpoc_motivo_corrections(driver_id);"
)
DDL_INDEX_TRACKING = (
    "CREATE INDEX IF NOT EXISTS idx_corrections_tracking "
    "ON fpoc_motivo_corrections(tracking_id);"
)


def main() -> int:
    print(f"[migrate] backend={backend()}")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(DDL_TABLE)
        cur.execute(DDL_INDEX_STATUS)
        cur.execute(DDL_INDEX_DRIVER)
        cur.execute(DDL_INDEX_TRACKING)
        cn.commit()
        print("[ddl] fpoc_motivo_corrections OK")

        cur.execute("SELECT COUNT(*) AS n FROM fpoc_motivo_corrections")
        n = int(cur.fetchone().n)
        cur.execute("SELECT COUNT(*) AS n FROM fpoc_motivo_corrections WHERE status = 'pending'")
        n_pending = int(cur.fetchone().n)
        print(f"[summary] fpoc_motivo_corrections: {n} filas, {n_pending} pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())

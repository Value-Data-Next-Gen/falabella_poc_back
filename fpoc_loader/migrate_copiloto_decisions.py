"""CR-013 — Migración 024: tabla `fpoc.copiloto_decisions`.

Persiste las decisiones del operador sobre las sugerencias del copiloto IA.
Sirve para:
  - fine-tunear el modelo de sugerencias con feedback real
  - medir utilidad (qué se acepta, qué se ignora, etc.)
  - auditoría: quién hizo qué y cuándo

Columnas:
  - decision_id     PK identity / autoincrement
  - created_at      timestamp UTC del registro
  - user_email      identidad del operador (string, no FK — sobrevive borrado de user)
  - empresa_id      empresa visible al operador en ese momento
  - fecha           día operativo al que se refiere la sugerencia
  - suggestion_id   slug de la sugerencia (texto, p.ej. 'retraso-vip-platinum')
  - intent          'escalate_supervisor' | 'retry_driver_alert' |
                    'review_visits' | 'mark_incident' | 'ignore'
  - tracking_id     NULL si la sugerencia no está atada a una visita puntual
  - payload_json    JSON serializado con severity / features / contexto

Índices:
  - (fecha)
  - (empresa_id, fecha)

Idempotente (chequea existencia antes de crear). Azure SQL único backend.

Uso a mano:
    python -m backend.fpoc_loader.migrate_copiloto_decisions
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


VALID_INTENTS = (
    "escalate_supervisor",
    "retry_driver_alert",
    "review_visits",
    "mark_incident",
    "ignore",
)


def _table_exists(cn, table: str) -> bool:
    cur = cn.cursor()
    cur.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?",
        table.replace("fpoc_", ""),
    )
    return cur.fetchone() is not None


def _create_copiloto_decisions(cn) -> None:
    if _table_exists(cn, "fpoc_copiloto_decisions"):
        print("[skip] fpoc.copiloto_decisions ya existe")
        return
    cur = cn.cursor()
    intents_list = "','".join(VALID_INTENTS)
    cur.execute(f"""
        CREATE TABLE fpoc.copiloto_decisions (
            decision_id     INT IDENTITY(1,1) PRIMARY KEY,
            created_at      DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
            user_email      NVARCHAR(256) NOT NULL,
            empresa_id      INT NULL,
            fecha           DATE NOT NULL,
            suggestion_id   NVARCHAR(128) NOT NULL,
            intent          NVARCHAR(64) NOT NULL
                CHECK (intent IN ('{intents_list}')),
            tracking_id     NVARCHAR(64) NULL,
            payload_json    NVARCHAR(MAX) NULL
        );
    """)
    cur.execute(
        "CREATE INDEX IX_copiloto_decisions_fecha "
        "ON fpoc.copiloto_decisions(fecha);"
    )
    cur.execute(
        "CREATE INDEX IX_copiloto_decisions_empresa_fecha "
        "ON fpoc.copiloto_decisions(empresa_id, fecha);"
    )
    cn.commit()
    print("[ok]   fpoc.copiloto_decisions creada con 2 índices")


def main(quiet: bool = False) -> int:
    if not quiet:
        print("[migrate] backend=sqlserver")
    with get_conn() as cn:
        _create_copiloto_decisions(cn)
    return 0


if __name__ == "__main__":
    sys.exit(main())

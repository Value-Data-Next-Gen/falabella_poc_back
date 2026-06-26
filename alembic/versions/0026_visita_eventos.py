"""CR-028 Part A: visita audit log.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-31 00:00:00+00:00

Motivation:
  CR-028 adds operator-facing actions on visitas (reorder, cancel with motivo,
  promote VIPs, move route, recalc ETAs). Each of these mutates state that
  downstream agents (planners, drivers, supervisors) need to audit. We add an
  append-only log table `td.visita_eventos` that captures every change with
  the actor, the change type and a JSON payload of the old/new values.

DDL:
  CREATE TABLE td.visita_eventos (
      evento_id     INT IDENTITY PK,
      visita_id     INT NOT NULL FK -> visitas(visita_id) ON DELETE CASCADE,
      tipo          NVARCHAR(40) NOT NULL CHECK (...),
      user_id       INT NULL FK -> users(user_id) ON DELETE SET NULL,
      payload_json  NVARCHAR(2000) NULL,
      created_at    DATETIME2 DEFAULT SYSUTCDATETIME()
  )
  + IX (visita_id, created_at DESC)

Idempotency: Alembic revision tracking. Test path (SQLite create_all) gets
the same shape via Base.metadata.

Downgrade: drops index + table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.core.config import settings

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def _qual(table: str) -> str:
    if IS_MSSQL:
        return f"[{SCHEMA}].[{table}]" if SCHEMA else f"[{table}]"
    return f'"{table}"'


def upgrade() -> None:
    op.create_table(
        "visita_eventos",
        sa.Column("evento_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("visita_id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.String(2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "tipo IN ('orden_change','estado_change','cancelada','ruta_change',"
            "'promoted_vip','eta_recalc')",
            name="ck_visita_eventos_tipo",
        ),
        schema=ref_schema,
    )

    # Hard FKs only on MSSQL — on SQLite the ORM declares them, and
    # create_all wires them automatically.
    if IS_MSSQL:
        op.execute(
            f"ALTER TABLE {_qual('visita_eventos')} "
            f"ADD CONSTRAINT fk_visita_eventos_visita_id_visitas "
            f"FOREIGN KEY (visita_id) REFERENCES {_qual('visitas')} (visita_id) "
            f"ON DELETE CASCADE"
        )
        op.execute(
            f"ALTER TABLE {_qual('visita_eventos')} "
            f"ADD CONSTRAINT fk_visita_eventos_user_id_users "
            f"FOREIGN KEY (user_id) REFERENCES {_qual('users')} (user_id) "
            f"ON DELETE SET NULL"
        )

    # (visita_id, created_at DESC) — supports the audit log query
    # GET /visitas/{visita_id}/eventos ORDER BY created_at DESC.
    if IS_MSSQL:
        op.execute(
            f"CREATE INDEX ix_visita_eventos_visita_id_created_at "
            f"ON {_qual('visita_eventos')} (visita_id, created_at DESC)"
        )
    else:
        op.create_index(
            "ix_visita_eventos_visita_id_created_at",
            "visita_eventos",
            ["visita_id", "created_at"],
            schema=ref_schema,
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.indexes "
            f"WHERE name='ix_visita_eventos_visita_id_created_at') "
            f"DROP INDEX ix_visita_eventos_visita_id_created_at "
            f"ON {_qual('visita_eventos')}"
        )
    else:
        op.drop_index(
            "ix_visita_eventos_visita_id_created_at",
            table_name="visita_eventos",
            schema=ref_schema,
        )
    op.drop_table("visita_eventos", schema=ref_schema)

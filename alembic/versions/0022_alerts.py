"""CR-022 Part A: alerts table.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-30 18:00:00+00:00

Adds `td.alerts` with check constraints on tipo/severity/estado, hard FKs to
empresas/dias_operativos/rutas/visitas/users, and 4 indexes including a filtered
unique on `dedupe_key` that excludes `descartada` rows. The filtered index is
the cron-side idempotency guard: it prevents creating two `eta_breach` alerts
for the same visita on the same sim_clock date, but does not block re-creation
after an operator dismisses one (descartada -> excluded from the unique).

SQLite (tests) skips the filtered index; we fall back to non-unique
`(dedupe_key)` index because SQLite supports filtered indexes only with the
column list literal form which Alembic doesn't emit portably.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.core.config import settings

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def _qual(table: str) -> str:
    if IS_MSSQL:
        return f"[{SCHEMA}].[{table}]"
    return f'"{table}"'


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("empresa_id", sa.Integer(), nullable=False),
        sa.Column("dia_id", sa.Integer(), nullable=True),
        sa.Column("ruta_id", sa.Integer(), nullable=True),
        sa.Column("visita_id", sa.Integer(), nullable=True),
        sa.Column("descripcion", sa.String(500), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "estado",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'abierta'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "notified_recipients_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.CheckConstraint(
            "tipo IN ('eta_breach','eta_preview','vip_deadline','manual')",
            name="ck_alerts_tipo",
        ),
        sa.CheckConstraint(
            "severity IN ('baja','media','alta','critica')",
            name="ck_alerts_severity",
        ),
        sa.CheckConstraint(
            "estado IN ('abierta','notificada','resuelta','descartada')",
            name="ck_alerts_estado",
        ),
        schema=ref_schema,
    )

    # Hard FKs (MSSQL only — declared in ORM metadata on SQLite-create-all).
    if IS_MSSQL:
        op.execute(
            f"ALTER TABLE {_qual('alerts')} ADD CONSTRAINT fk_alerts_empresa_id_empresas "
            f"FOREIGN KEY (empresa_id) REFERENCES {_qual('empresas')} (empresa_id) "
            "ON DELETE NO ACTION"
        )
        op.execute(
            f"ALTER TABLE {_qual('alerts')} ADD CONSTRAINT fk_alerts_dia_id_dias_operativos "
            f"FOREIGN KEY (dia_id) REFERENCES {_qual('dias_operativos')} (dia_id) "
            "ON DELETE CASCADE"
        )
        op.execute(
            f"ALTER TABLE {_qual('alerts')} ADD CONSTRAINT fk_alerts_ruta_id_rutas "
            f"FOREIGN KEY (ruta_id) REFERENCES {_qual('rutas')} (ruta_id) "
            "ON DELETE NO ACTION"
        )
        op.execute(
            f"ALTER TABLE {_qual('alerts')} ADD CONSTRAINT fk_alerts_visita_id_visitas "
            f"FOREIGN KEY (visita_id) REFERENCES {_qual('visitas')} (visita_id) "
            "ON DELETE NO ACTION"
        )
        op.execute(
            f"ALTER TABLE {_qual('alerts')} ADD CONSTRAINT fk_alerts_resolved_by_user_id_users "
            f"FOREIGN KEY (resolved_by_user_id) REFERENCES {_qual('users')} (user_id) "
            "ON DELETE SET NULL"
        )

    # Indexes.
    op.create_index(
        "ix_alerts_empresa_estado",
        "alerts",
        ["empresa_id", "estado"],
        schema=ref_schema,
    )
    op.create_index(
        "ix_alerts_dia_estado",
        "alerts",
        ["dia_id", "estado"],
        schema=ref_schema,
    )
    op.create_index(
        "ix_alerts_created_at_desc",
        "alerts",
        [sa.text("created_at DESC")] if IS_MSSQL else ["created_at"],
        schema=ref_schema,
    )
    if IS_MSSQL:
        # Filtered unique — open/notificada/resuelta share the namespace but
        # descartada are excluded so dismissed dedupe keys can be re-issued.
        op.execute(
            f"CREATE UNIQUE INDEX uq_alerts_dedupe_key "
            f"ON {_qual('alerts')} (dedupe_key) "
            f"WHERE dedupe_key IS NOT NULL AND estado != 'descartada'"
        )
    else:
        # SQLite fallback: non-unique. Tests don't exercise dedupe at the DB
        # constraint layer — the application's pre-insert SELECT handles it.
        op.create_index(
            "ix_alerts_dedupe_key", "alerts", ["dedupe_key"], schema=ref_schema
        )


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(
            f"IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'uq_alerts_dedupe_key') "
            f"DROP INDEX uq_alerts_dedupe_key ON {_qual('alerts')}"
        )
    else:
        op.drop_index("ix_alerts_dedupe_key", table_name="alerts", schema=ref_schema)

    op.drop_index("ix_alerts_created_at_desc", table_name="alerts", schema=ref_schema)
    op.drop_index("ix_alerts_dia_estado", table_name="alerts", schema=ref_schema)
    op.drop_index("ix_alerts_empresa_estado", table_name="alerts", schema=ref_schema)

    op.drop_table("alerts", schema=ref_schema)

"""Clientes master + Falabella-source fields on rutas/visitas + seed empresas 4 & 5.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-30 00:00:00+00:00

Changes:
  - Seed `td.empresas` rows 4 (Transportes Cordillera) and 5 (RutaSur Express)
    used by the Falabella ingest mapping FAL 27 -> 4, FAL 33 -> 5.
  - Create `td.clientes` master table (cliente_id PK, empresa_id FK, RUT, VIP).
  - Add `td.rutas.folio`, `td.rutas.subfolio` + non-unique index (dia_id, folio).
  - Add `td.visitas` columns: cliente_id (FK), folio_cliente, subfolio_bulto,
    parent_order, tipo_documento, region, fecha_pactada, estado_fuente.

Idempotency:
  - Empresas inserts use `WHERE NOT EXISTS`.
  - All ALTERs/CREATEs assume fresh upgrade (no IF NOT EXISTS guards beyond
    empresas — Alembic tracks revisions and won't replay).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None


def _fk(table: str, col: str) -> str:
    return f"{SCHEMA + '.' if IS_MSSQL and SCHEMA else ''}{table}.{col}"


def upgrade() -> None:
    # ── 1. Seed empresas 4 & 5 (idempotent) ─────────────────────────────
    if IS_MSSQL:
        op.execute(
            f"IF NOT EXISTS (SELECT 1 FROM [{SCHEMA}].[empresas] WHERE empresa_id = 4) "
            f"BEGIN "
            f"SET IDENTITY_INSERT [{SCHEMA}].[empresas] ON; "
            f"INSERT INTO [{SCHEMA}].[empresas] (empresa_id, nombre, razon_social, rut, central_phone, supervisor_phone_e164, activo) "
            f"VALUES (4, N'Transportes Cordillera', N'Transportes Cordillera Limitada', N'77.123.456-8', N'+56224445555', N'+56987776655', 1); "
            f"SET IDENTITY_INSERT [{SCHEMA}].[empresas] OFF; "
            f"END"
        )
        op.execute(
            f"IF NOT EXISTS (SELECT 1 FROM [{SCHEMA}].[empresas] WHERE empresa_id = 5) "
            f"BEGIN "
            f"SET IDENTITY_INSERT [{SCHEMA}].[empresas] ON; "
            f"INSERT INTO [{SCHEMA}].[empresas] (empresa_id, nombre, razon_social, rut, central_phone, supervisor_phone_e164, activo) "
            f"VALUES (5, N'RutaSur Express', N'RutaSur Express SpA', N'77.234.567-9', N'+56223334444', N'+56988887777', 1); "
            f"SET IDENTITY_INSERT [{SCHEMA}].[empresas] OFF; "
            f"END"
        )

    # ── 2. Create td.clientes ───────────────────────────────────────────
    op.create_table(
        "clientes",
        sa.Column("cliente_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id",
            sa.Integer(),
            sa.ForeignKey(
                _fk("empresas", "empresa_id"),
                name="fk_clientes_empresa_id_empresas",
                ondelete="CASCADE",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("rut", sa.String(20), nullable=True),
        sa.Column("telefono", sa.String(20), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("es_vip", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("vip_razon", sa.String(500), nullable=True),
        sa.Column("notas_operativas", sa.String(1000), nullable=True),
        sa.Column("direccion_default", sa.String(300), nullable=True),
        sa.Column("comuna_default", sa.String(100), nullable=True),
        sa.Column("region_default", sa.String(100), nullable=True),
        sa.Column("lat_default", sa.Float(), nullable=True),
        sa.Column("lon_default", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=ref_schema,
    )

    # Filtered unique on (empresa_id, rut) when rut is not null.
    if IS_MSSQL:
        op.execute(
            f"CREATE UNIQUE INDEX uq_clientes_empresa_rut "
            f"ON [{SCHEMA}].[clientes] (empresa_id, rut) WHERE rut IS NOT NULL"
        )

    # ── 3. ALTER td.rutas: folio, subfolio ──────────────────────────────
    op.add_column(
        "rutas",
        sa.Column("folio", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "rutas",
        sa.Column("subfolio", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.create_index(
        "ix_rutas_dia_folio",
        "rutas",
        ["dia_id", "folio"],
        unique=False,
        schema=ref_schema,
    )

    # ── 4. ALTER td.visitas: Falabella-source fields ────────────────────
    op.add_column(
        "visitas",
        sa.Column("cliente_id", sa.Integer(), nullable=True),
        schema=ref_schema,
    )
    if IS_MSSQL:
        op.create_foreign_key(
            "fk_visitas_cliente_id_clientes",
            "visitas",
            "clientes",
            ["cliente_id"],
            ["cliente_id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_visitas_cliente_id",
        "visitas",
        ["cliente_id"],
        unique=False,
        schema=ref_schema,
    )

    op.add_column(
        "visitas",
        sa.Column("folio_cliente", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "visitas",
        sa.Column("subfolio_bulto", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "visitas",
        sa.Column("parent_order", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "visitas",
        sa.Column("tipo_documento", sa.String(50), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "visitas",
        sa.Column("region", sa.String(100), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "visitas",
        sa.Column("fecha_pactada", sa.Date(), nullable=True),
        schema=ref_schema,
    )
    op.add_column(
        "visitas",
        sa.Column("estado_fuente", sa.String(50), nullable=True),
        schema=ref_schema,
    )


def downgrade() -> None:
    # Reverse order: drop visitas cols → indexes → rutas cols → clientes → empresas rows.
    if IS_MSSQL:
        op.drop_constraint("fk_visitas_cliente_id_clientes", "visitas", schema=SCHEMA, type_="foreignkey")
    op.drop_index("ix_visitas_cliente_id", table_name="visitas", schema=ref_schema)
    for col in (
        "estado_fuente",
        "fecha_pactada",
        "region",
        "tipo_documento",
        "parent_order",
        "subfolio_bulto",
        "folio_cliente",
        "cliente_id",
    ):
        op.drop_column("visitas", col, schema=ref_schema)

    op.drop_index("ix_rutas_dia_folio", table_name="rutas", schema=ref_schema)
    op.drop_column("rutas", "subfolio", schema=ref_schema)
    op.drop_column("rutas", "folio", schema=ref_schema)

    if IS_MSSQL:
        op.execute(f"DROP INDEX uq_clientes_empresa_rut ON [{SCHEMA}].[clientes]")
    op.drop_table("clientes", schema=ref_schema)

    if IS_MSSQL:
        op.execute(f"DELETE FROM [{SCHEMA}].[empresas] WHERE empresa_id IN (4, 5)")

"""Motivos de no-entrega catalog table + seed from v1.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-26 00:00:00+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")
ref_schema = SCHEMA if IS_MSSQL else None

SEED = [
    (1, "SIN MORADORES", "Al llegar, no hay nadie que reciba el paquete. Cliente ausente, inmueble deshabitado, reagenda.", "Solo si nadie atiende. Si el problema es la direccion -> PROBLEMA DE DIRECCION. Si rechazan -> CLIENTE RECHAZA. Si no conocen -> NO CONOCEN A CLIENTE.", "low", 0),
    (2, "NO CONOCEN A CLIENTE", "Las personas en la direccion no conocen al destinatario.", "Solo si hay personas pero no conocen al destinatario. No si nadie habia -> SIN MORADORES.", "low", 0),
    (3, "PROBLEMA DE DIRECCION/ SIN INFORMACION", "Direccion incorrecta, incompleta o imposible de localizar.", "Direccion mala, no existe, incompleta. No si la zona no se atiende -> NO DESPACHA A LOCALIDAD.", "medium", 1),
    (4, "NO DESPACHA A LOCALIDAD", "Direccion fuera de la zona que la empresa atiende.", None, "low", 0),
    (5, "FUERA DE COBERTURA/ FRECUENCIA", "Zona fuera del alcance de cobertura o frecuencia de visita.", None, "low", 0),
    (6, "PROD NO ENTREGADO POR TIEMPO", "No se pudo entregar dentro del tiempo limite. Trafico, demoras.", "No si hubo siniestro -> SINIESTRO EN CALLE. No si el producto nunca subio -> PRODUCTO NO CARGADO.", "low", 0),
    (7, "PRODUCTO NO CARGADO", "Paquete no fue cargado en el vehiculo desde el origen.", None, "high", 1),
    (8, "CLIENTE RECHAZA", "Cliente rechaza recibir el paquete.", "Solo si el cliente atendio y rechaza. Si anula por direccion mala -> PROBLEMA DE DIRECCION.", "low", 0),
    (9, "SINIESTRO EN CALLE", "Accidente, manifestacion, cierre de calles o clima adverso.", None, "critical", 1),
    (10, "PRODUCTO CON PROBLEMAS", "Producto con defectos, danos o problemas.", None, "medium", 0),
    (11, "NO CUMPLE CONDICIONES RETIRO", "Condiciones inadecuadas para entrega/retiro. Falta espacio, acceso.", None, "low", 0),
    (12, "PRODUCTO ROBADO", "Paquete robado durante el proceso de entrega.", None, "critical", 1),
    (13, "RIESGO FRAUDE", "Pedido sospechoso de fraude. NO entregar, reportar inmediatamente.", "No si el cliente solo rechaza por arrepentimiento -> CLIENTE RECHAZA.", "critical", 1),
    (14, "DETENCION URGENTE", "Detencion ordenada por Falabella. NO entregar, devolver al CD.", "No si el cliente rechaza personalmente -> CLIENTE RECHAZA.", "high", 1),
]


def upgrade() -> None:
    op.create_table(
        "motivos",
        sa.Column("motivo_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("codigo", sa.String(100), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(500), nullable=False),
        sa.Column("desambiguacion", sa.String(1000), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False, server_default=sa.text("'low'")),
        sa.Column("alertable", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=ref_schema,
    )

    for orden, codigo, desc, desamb, sev, alert in SEED:
        desamb_val = f"'{desamb}'" if desamb else "NULL"
        alert_val = 1 if alert else 0
        if IS_MSSQL:
            op.execute(
                f"INSERT INTO [{SCHEMA}].[motivos] (codigo, descripcion, desambiguacion, severity, alertable, orden) "
                f"VALUES ('{codigo}', '{desc}', {desamb_val}, '{sev}', {alert_val}, {orden})"
            )


def downgrade() -> None:
    op.drop_table("motivos", schema=ref_schema)

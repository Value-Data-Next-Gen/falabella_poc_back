"""vw_notif_recipients (td.vw_notif_recipients)

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-24 00:00:00+00:00

THE star view that resolves v1's "3 separate tables, 50 undelivered to ghost
recipients" bug. From CR-010 onward, every dispatcher / cron reads ONLY this
view to decide who receives a WhatsApp.

Schema:
  recipient_type   'user' | 'driver' | 'contacto'
  recipient_id     string (works for both INT and string driver_ids)
  nombre           display name
  phone_e164       E.164 phone (or NULL)
  empresa_id       INT (NULL for cross-empresa Falabella users)
  rol_or_role      'falabella_admin'|'falabella_ops'|'transport_manager'|'jefe'|'coordinador'|...
  opted_in_at      timestamp or NULL
  notify_enabled   BIT 1 iff phone valid + opted-in + activo
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.config import settings


revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = settings.db_schema
IS_MSSQL = "mssql" in (op.get_bind().dialect.name or "")


_VIEW_SQL_MSSQL = f"""
CREATE VIEW [{SCHEMA}].[vw_notif_recipients] AS
SELECT
    CAST('user' AS NVARCHAR(20))            AS recipient_type,
    CAST(u.user_id AS NVARCHAR(40))         AS recipient_id,
    u.display_name                          AS nombre,
    u.phone_e164                            AS phone_e164,
    u.empresa_id                            AS empresa_id,
    u.role                                  AS rol_or_role,
    u.activation_used_at                    AS opted_in_at,
    CAST(CASE
        WHEN u.notify_whatsapp = 1
         AND u.activation_used_at IS NOT NULL
         AND u.phone_e164 LIKE '+%'
         AND u.activo = 1
        THEN 1 ELSE 0
    END AS BIT) AS notify_enabled
FROM [{SCHEMA}].[users] u

UNION ALL

SELECT
    CAST('driver' AS NVARCHAR(20)),
    CAST(d.driver_id AS NVARCHAR(40)),
    d.nombre,
    d.phone_e164,
    d.empresa_id,
    CAST('driver' AS NVARCHAR(50)),
    d.opted_in_at,
    CAST(CASE
        WHEN d.notify_whatsapp = 1
         AND d.opted_in_at IS NOT NULL
         AND d.phone_e164 LIKE '+%'
         AND d.activo = 1
        THEN 1 ELSE 0
    END AS BIT)
FROM [{SCHEMA}].[drivers] d

UNION ALL

SELECT
    CAST('contacto' AS NVARCHAR(20)),
    CAST(c.contact_id AS NVARCHAR(40)),
    c.nombre,
    c.phone_e164,
    c.empresa_id,
    c.rol,
    c.opted_in_at,
    CAST(CASE
        WHEN c.opted_in_at IS NOT NULL
         AND c.phone_e164 LIKE '+%'
         AND c.activo = 1
        THEN 1 ELSE 0
    END AS BIT)
FROM [{SCHEMA}].[empresa_contactos] c
"""


def upgrade() -> None:
    if IS_MSSQL:
        op.execute(f"IF OBJECT_ID('[{SCHEMA}].[vw_notif_recipients]', 'V') IS NOT NULL "
                   f"DROP VIEW [{SCHEMA}].[vw_notif_recipients]")
        op.execute(_VIEW_SQL_MSSQL)
    # On SQLite (tests) we skip — the view is MSSQL-only for now.


def downgrade() -> None:
    if IS_MSSQL:
        op.execute(f"IF OBJECT_ID('[{SCHEMA}].[vw_notif_recipients]', 'V') IS NOT NULL "
                   f"DROP VIEW [{SCHEMA}].[vw_notif_recipients]")

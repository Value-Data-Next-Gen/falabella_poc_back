"""SQLAlchemy declarative `Base` with consistent naming conventions.

Naming conventions matter because Alembic's autogenerate uses them to produce
deterministic constraint/index names across runs. Without them, MS SQL Server's
auto-generated names like `PK__users__abc123` change between environments and
break migrations.

Convention follows Alembic's official "Working with autogenerate" recommendation
(alembic.sqlalchemy.org/en/latest/naming.html).
"""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    Models go under `app/db/models/*.py`. Import them via `app.db.models`
    package so Alembic's autogenerate discovers their metadata.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

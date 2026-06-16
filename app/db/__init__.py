"""DB layer — async engine, sessionmaker, base, mixins.

Models live under `app/db/models/` (added CR-004 onwards). Import them through
this package so Alembic's autogenerate sees the metadata.
"""
from app.db.base import Base
from app.db.mixins import AuditedByMixin, SoftDeleteMixin, TimestampedMixin
from app.db.session import (
    dispose_engine,
    get_db,
    get_engine,
    get_sessionmaker,
    ping_db,
)

__all__ = [
    "AuditedByMixin",
    "Base",
    "SoftDeleteMixin",
    "TimestampedMixin",
    "dispose_engine",
    "get_db",
    "get_engine",
    "get_sessionmaker",
    "ping_db",
]

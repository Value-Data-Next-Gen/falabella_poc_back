"""Reusable SQLAlchemy mixins. Apply via multiple inheritance.

Example:
    class User(Base, TimestampedMixin, SoftDeleteMixin):
        __tablename__ = "users"
        ...
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.orm import Mapped, declarative_mixin, mapped_column


@declarative_mixin
class TimestampedMixin:
    """`created_at` + `updated_at` server-defaulted to UTC now."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@declarative_mixin
class SoftDeleteMixin:
    """`deleted_at` nullable. Query with `Model.deleted_at.is_(None)` to filter."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


@declarative_mixin
class AuditedByMixin:
    """`created_by_user_id` + `updated_by_user_id` FK to users.user_id.

    The FK constraint is added lazily by Alembic — the column is just an Integer
    here so the mixin works even on the `users` table itself (no circular FK).
    """

    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
    )

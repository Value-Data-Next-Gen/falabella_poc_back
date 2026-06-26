"""Unit tests for ORM mixins (no DB required — just metadata introspection)."""
from __future__ import annotations

from datetime import datetime

from app.db.base import NAMING_CONVENTION
from app.db.mixins import AuditedByMixin, SoftDeleteMixin, TimestampedMixin
from sqlalchemy import MetaData, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _temp_base() -> type:
    """Build a throwaway DeclarativeBase so model definitions in this test file
    don't pollute the app's global Base metadata."""

    class _TestBase(DeclarativeBase):
        metadata = MetaData(naming_convention=NAMING_CONVENTION)

    return _TestBase


def test_timestamped_mixin_adds_created_and_updated() -> None:
    Base = _temp_base()

    class Foo(Base, TimestampedMixin):
        __tablename__ = "foo"
        id: Mapped[int] = mapped_column(primary_key=True)

    cols = {c.name for c in inspect(Foo).columns}
    assert "created_at" in cols
    assert "updated_at" in cols


def test_soft_delete_mixin_adds_deleted_at() -> None:
    Base = _temp_base()

    class Bar(Base, SoftDeleteMixin):
        __tablename__ = "bar"
        id: Mapped[int] = mapped_column(primary_key=True)

    cols = {c.name for c in inspect(Bar).columns}
    assert "deleted_at" in cols


def test_audited_by_mixin_adds_user_ids() -> None:
    Base = _temp_base()

    class Baz(Base, AuditedByMixin):
        __tablename__ = "baz"
        id: Mapped[int] = mapped_column(primary_key=True)

    cols = {c.name for c in inspect(Baz).columns}
    assert "created_by_user_id" in cols
    assert "updated_by_user_id" in cols


def test_naming_convention_applied() -> None:
    """PK / FK / UQ names follow the documented convention.

    `Alembic autogenerate` relies on this to keep names stable across runs.
    """
    Base = _temp_base()

    class Qux(Base):
        __tablename__ = "qux"
        id: Mapped[int] = mapped_column(primary_key=True)

    pk_columns = list(Qux.__table__.primary_key.columns)
    assert pk_columns[0].name == "id"
    # The PK constraint name itself follows convention "pk_<table>"
    assert Qux.__table__.primary_key.name == "pk_qux"


def test_soft_delete_is_deleted_property() -> None:
    """Sanity: instance method without DB hit."""
    Base = _temp_base()

    class Quux(Base, SoftDeleteMixin):
        __tablename__ = "quux"
        id: Mapped[int] = mapped_column(primary_key=True)

    q = Quux()
    assert q.is_deleted is False
    q.deleted_at = datetime(2026, 1, 1)
    assert q.is_deleted is True

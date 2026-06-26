"""Scope filter for SQLAlchemy queries.

Multi-tenancy rule:
  - `falabella_admin` and `falabella_ops` see ALL empresas.
  - Other users see only the empresas in their `user_empresas` junction table.
  - If no empresas assigned → empty result set.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import false

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

    from app.db.models.user import User


def _get_empresa_ids(user: User) -> list[int]:
    return getattr(user, '_empresa_ids', [])


def apply_scope(stmt: Select[Any], user: User, col: Any) -> Select[Any]:
    if user.role in ("falabella_admin", "falabella_ops"):
        return stmt
    ids = _get_empresa_ids(user)
    if not ids:
        return stmt.where(false())
    return stmt.where(col.in_(ids))


def can_access_empresa(user: User, empresa_id: int) -> bool:
    if user.role in ("falabella_admin", "falabella_ops"):
        return True
    return empresa_id in _get_empresa_ids(user)

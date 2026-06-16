"""baseline (empty)

Revision ID: 0001
Revises:
Create Date: 2026-05-24 00:00:00+00:00

The baseline migration stamps the empty schema. Real DDL begins in 0002+
(users, drivers, etc. added in CR-004+).
"""
from __future__ import annotations

from collections.abc import Sequence


revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op — establishes the baseline revision id."""
    pass


def downgrade() -> None:
    """No-op — there is no prior state."""
    pass

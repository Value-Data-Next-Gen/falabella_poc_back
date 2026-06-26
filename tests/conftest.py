"""Shared pytest fixtures.

  - Adds project-root pythonpath.
  - Provides minimum env so `Settings` doesn't fail-fast in unit tests.
  - Tests do NOT touch the DB. Integration verified live against Azure SQL.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def _set_test_env() -> None:
    """Minimum env so `Settings` doesn't fail-fast in unit tests.

    NO DB connection here. Unit tests only exercise pure logic.
    Live integration is verified via `curl` against the real Azure SQL `td.*` schema.
    """
    os.environ.setdefault(
        "JWT_SECRET",
        "test-secret-token-only-for-pytest-environment-pad-32",
    )
    os.environ.setdefault("DB_SERVER", "tcp:localhost,1433")
    os.environ.setdefault("DB_NAME", "test")
    os.environ.setdefault("DB_USER", "test")
    os.environ.setdefault("DB_PASSWORD", "test-pwd")
    os.environ.setdefault("NOTIFICATIONS_DRY_RUN", "true")
    os.environ.setdefault("DB_SCHEMA", "")


_set_test_env()

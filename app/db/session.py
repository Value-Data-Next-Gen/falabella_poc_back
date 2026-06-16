"""Async DB engine + sessionmaker + FastAPI dependency.

Production: `mssql+aioodbc` against Azure SQL.
Tests: `sqlite+aiosqlite:///:memory:` (no driver setup needed; CI-friendly).

The engine is created lazily on first call to `get_engine()`. The lifespan in
`app/main.py` triggers initialization at startup and runs a `SELECT 1` smoke
check; the result toggles `app.state.db_ready`.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

if TYPE_CHECKING:
    pass


_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def _build_url() -> str:
    """Pick the DB URL. If `DB_TEST_URL` is set (tests / CI), use it; else prod URL.

    Setting `DB_TEST_URL=sqlite+aiosqlite:///:memory:` is the canonical way to run
    unit/integration tests without spinning up Azure SQL.
    """
    test_url = os.environ.get("DB_TEST_URL")
    if test_url:
        return test_url
    return settings.db_url_async


def get_engine() -> AsyncEngine:
    """Lazily create + cache the async engine. Safe to call from any worker."""
    global _engine, _SessionLocal
    if _engine is None:
        url = _build_url()
        is_sqlite = url.startswith("sqlite")
        # SQLite (aiosqlite) uses StaticPool — pool_size / max_overflow are invalid.
        # Only pass pooling kwargs for non-SQLite engines.
        engine_kwargs: dict = {"echo": False, "future": True}
        if not is_sqlite:
            engine_kwargs.update(
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
            )
        _engine = create_async_engine(url, **engine_kwargs)

        # CR-019 fix: enable pyodbc fast_executemany so bulk INSERT (executemany)
        # sends rows as a single batched RPC instead of one round-trip per row.
        # Without this, ingesting ~2k rows to Azure SQL takes >10 min.
        #
        # Under mssql+aioodbc, the cursor passed to the event hook is the
        # SQLAlchemy AsyncAdapt_dbapi_cursor wrapper around aioodbc.Cursor
        # around pyodbc.Cursor. The setter must reach the real pyodbc cursor.
        if url.startswith("mssql"):
            @event.listens_for(_engine.sync_engine, "before_cursor_execute")
            def _set_fast_executemany(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
                if not executemany:
                    return
                if not statement.lstrip().upper().startswith("INSERT"):
                    return
                # Unwrap SQLAlchemy async adapter → aioodbc.Cursor → pyodbc.Cursor.
                inner = getattr(cursor, "_cursor", cursor)
                impl = getattr(inner, "_impl", inner)
                try:
                    impl.fast_executemany = True
                except AttributeError:
                    logger.debug("fast_executemany unsupported by underlying cursor")

        _SessionLocal = async_sessionmaker(
            _engine, expire_on_commit=False, autoflush=False, class_=AsyncSession,
        )
        logger.info(f"DB engine initialized: {url.split('@')[-1] if '@' in url else url}")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Returns the cached sessionmaker. Calls `get_engine()` to ensure init."""
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


async def dispose_engine() -> None:
    """Tear down the engine. Called from lifespan shutdown."""
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
        logger.info("DB engine disposed")
    _engine = None
    _SessionLocal = None


async def ping_db() -> bool:
    """Run `SELECT 1`. Returns True on success, False on any error.

    Called from lifespan to set the `app.state.db_ready` flag.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            row = result.scalar_one()
            return row == 1
    except Exception as e:
        logger.warning(f"DB ping failed: {e}")
        return False


# ----------------------------------------------------------------------------
# FastAPI dependency
# ----------------------------------------------------------------------------

async def get_db() -> AsyncIterator[AsyncSession]:
    """Yields an `AsyncSession` scoped to a request. Auto-closes on exit."""
    sm = get_sessionmaker()
    async with sm() as session:
        yield session

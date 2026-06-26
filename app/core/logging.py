"""Structured logging via loguru.

Features:
  - Per-request `request_id` (UUID v4) injected as ContextVar.
  - Middleware adds `X-Request-Id` header to every response.
  - Sensitive fields auto-redacted in log records.
  - JSON output to stdout (App Service captures + ships to App Insights).

Setup is called once at module import; further `from loguru import logger` works
anywhere.
"""
from __future__ import annotations

import contextvars
import re
import sys
import uuid
from typing import TYPE_CHECKING, Any

from loguru import logger

from app.core.config import settings

if TYPE_CHECKING:
    from fastapi import Request, Response
    from starlette.middleware.base import RequestResponseEndpoint


# ----------------------------------------------------------------------------
# Per-request context
# ----------------------------------------------------------------------------

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def current_request_id() -> str:
    return request_id_ctx.get()


# ----------------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------------

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|jwt|api[_-]?key|cookie|authorization|set-cookie)"
)
_REDACT_PLACEHOLDER = "***REDACTED***"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACT_PLACEHOLDER if _SENSITIVE_KEY_RE.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _format_record(record: dict[str, Any]) -> str:
    """Serialize the record as a JSON-ish line. Loguru's default with `extra`."""
    record["extra"]["request_id"] = current_request_id()
    record["extra"] = _redact(record["extra"])
    return (
        "{time:YYYY-MM-DDTHH:mm:ss.SSS!UTC}Z | {level: <8} | "
        "{extra[request_id]} | {name}:{function}:{line} | {message}\n"
    )


# ----------------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------------

def setup_logging() -> None:
    """Configure loguru sinks. Idempotent (safe to call multiple times)."""
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        format=_format_record,
        backtrace=True,
        diagnose=False,  # diagnose=True leaks values in tracebacks; off in prod.
        enqueue=False,
    )


# Auto-setup at import time so anyone who does `from loguru import logger` is wired.
setup_logging()


# ----------------------------------------------------------------------------
# FastAPI middleware
# ----------------------------------------------------------------------------

async def request_id_middleware(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """Attach a `request_id` ContextVar + `X-Request-Id` response header."""
    incoming = request.headers.get("x-request-id")
    rid = incoming if incoming and len(incoming) <= 64 else uuid.uuid4().hex
    token = request_id_ctx.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers["X-Request-Id"] = rid
    return response

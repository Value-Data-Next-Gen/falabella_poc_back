"""Security-headers middleware.

Adds a baseline of defensive response headers (the set CLAUDE.md §5.6 documents
as enforced). We intentionally do NOT set a strict `default-src` CSP here: this
app also serves the SPA, whose map tiles / deck.gl assets would break under one.
Clickjacking is covered via `frame-ancestors 'none'` + `X-Frame-Options`.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

SECURITY_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


async def security_headers_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response

"""API v1 routers. Each domain is one module; main.py registers all of them."""

from app.api.v1 import health

__all__ = ["health"]

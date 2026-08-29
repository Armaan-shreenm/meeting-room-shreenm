"""HTTP layer. Every route in this package is mounted under /api."""

from app.api.router import api_router

__all__ = ["api_router"]

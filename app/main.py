"""FastAPI application.

One process serves everything. The API lives under ``/api`` and the approved
frontend is served as static files from ``/``, so the browser calls the API with
relative paths and there is no second origin, no CORS in production and no
separate frontend host to deploy.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.config import settings
from app.core.logging import configure_logging

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Log the resolved configuration once, on a cold start.

    No connection pool is warmed and no background thread is started: Render may
    stop and restart this process at any time, so nothing that matters may live
    only in memory.
    """
    configure_logging()
    logger.info(
        "NM Meet %s starting — environment=%s branch=%s timezone=%s hours=%s-%s",
        __version__,
        settings.environment,
        settings.branch_name,
        settings.tz,
        settings.open_time.isoformat(timespec="minutes"),
        settings.close_time.isoformat(timespec="minutes"),
    )
    yield
    logger.info("NM Meet shutting down")


app = FastAPI(
    title="NM Meet",
    description=(
        "Meeting room booking for Shree NM, Mumbai branch. "
        "Five rooms, per-room availability, no double bookings."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# Permissive in development, where the frontend may be opened from a file or a
# second port. Effectively unused in production: the API and the page it serves
# share one origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API first. The static mount below is a catch-all and would otherwise swallow
# these paths.
app.include_router(api_router, prefix="/api")

# The frontend. ``html=True`` serves index.html for "/" and returns 404.html or a
# plain 404 for anything unknown.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    # Local convenience only. In production start.sh runs uvicorn directly.
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=not settings.is_production,
    )

"""Health check — Render's ``healthCheckPath``.

A deploy is only healthy if the application can reach its database. An instance
that cannot talk to PostgreSQL cannot answer a single useful request, so this
endpoint answers 503 in that case and Render keeps the previous deploy serving
rather than replacing it with a broken one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.config import settings
from app.core.time import now_local, now_utc
from app.database import engine
from app.schemas.health import DatabaseHealth, HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_database() -> DatabaseHealth:
    """One cheap round trip to confirm the connection actually works."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("Health check could not reach the database: %s", exc)
        return DatabaseHealth(
            connected=False,
            dialect=engine.dialect.name,
            error=type(exc).__name__,
        )
    return DatabaseHealth(connected=True, dialect=engine.dialect.name)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Application and database health",
)
def health(response: Response) -> HealthResponse:
    """Report application status and database connectivity."""
    database = _check_database()

    if not database.connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if database.connected else "degraded",
        application="NM Meet",
        version=__version__,
        environment=settings.environment,
        branch=settings.branch_name,
        timezone=settings.tz,
        time_utc=now_utc(),
        time_local=now_local(),
        database=database,
    )

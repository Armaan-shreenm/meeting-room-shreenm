"""Response models for the health endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DatabaseHealth(BaseModel):
    """Result of a single round trip to PostgreSQL."""

    connected: bool = Field(description="True when SELECT 1 succeeded.")
    dialect: str = Field(description="SQLAlchemy dialect in use.")
    error: str | None = Field(
        default=None, description="Failure reason when connected is false."
    )


class HealthResponse(BaseModel):
    """Payload served at ``/api/health``.

    Render polls this endpoint to decide whether a deploy is live, so it reports
    database connectivity rather than only that the process is running.
    """

    status: Literal["ok", "degraded"]
    application: str
    version: str
    environment: str
    branch: str
    timezone: str
    time_utc: datetime
    time_local: datetime
    database: DatabaseHealth

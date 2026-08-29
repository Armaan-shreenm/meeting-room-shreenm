"""Pydantic v2 request and response models."""

from app.schemas.health import DatabaseHealth, HealthResponse

__all__ = ["DatabaseHealth", "HealthResponse"]

"""Pydantic v2 request and response models."""

from app.schemas.availability import (
    AvailabilityResponse,
    BookingOnGrid,
    ExitCapResponse,
    RoomAvailability,
)
from app.schemas.bookings import AttendeeOut, BookingCreate, BookingDetail
from app.schemas.health import DatabaseHealth, HealthResponse
from app.schemas.reference import DepartmentOut, DirectoryUserOut, RoomOut

__all__ = [
    "AttendeeOut",
    "AvailabilityResponse",
    "BookingCreate",
    "BookingDetail",
    "BookingOnGrid",
    "DatabaseHealth",
    "DepartmentOut",
    "DirectoryUserOut",
    "ExitCapResponse",
    "HealthResponse",
    "RoomAvailability",
    "RoomOut",
]

"""Booking request and detail payloads."""

from __future__ import annotations

from datetime import date as date_type

from pydantic import BaseModel, ConfigDict, Field


class AttendeeOut(BaseModel):
    """An invited colleague and their reply."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    email: str
    response_status: str


class BookingCreate(BaseModel):
    """POST /api/bookings.

    ``department_id`` and ``conducted_by`` are optional here on purpose. A
    missing value must produce section 10's "Please choose a department." rather
    than Pydantic's "field required", so the service validates them and raises
    the specified message.
    """

    room_id: str
    date: date_type
    entry: str = Field(description='Local start, "13:00".', examples=["13:00"])
    exit: str = Field(description='Local end, "15:00".', examples=["15:00"])
    department_id: int | None = None
    conducted_by: int | None = None
    attendee_ids: list[int] = Field(default_factory=list)
    title: str = Field(default="", max_length=120)
    reception_note: str | None = None


class BookingDetail(BaseModel):
    """GET /api/bookings/{id} — the detail panel payload.

    Carries the permission flags so the frontend can hide what this viewer may
    not do. The server refuses the action regardless of what the frontend shows.
    """

    id: str
    room_id: str
    room_name: str
    colour_var: str
    date: date_type
    entry: str
    exit: str
    title: str
    department_id: int
    department: str
    conducted_by_id: int
    host: str
    booked_by_id: int
    booked_by: str
    reception_note: str | None
    status: str
    attendees: list[AttendeeOut]
    can_cancel: bool = Field(
        description="Whether the requesting user may cancel — spec section 9."
    )
    can_edit: bool = Field(
        description="Whether the requesting user may edit the details."
    )

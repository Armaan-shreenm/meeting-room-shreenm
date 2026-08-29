"""Booking request and detail payloads."""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

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


class BookingUpdate(BaseModel):
    """PATCH /api/bookings/{id} — details only.

    ``room_id``, ``date``, ``entry`` and ``exit`` are declared so that sending
    one produces the specific "cancel and re-book" instruction rather than being
    silently ignored. They are never applied.

    Every other field is optional, and "not sent" differs from "sent as null":
    omitting ``reception_note`` leaves it alone, sending ``null`` clears it.
    """

    title: str | None = Field(default=None, max_length=120)
    department_id: int | None = None
    conducted_by: int | None = None
    attendee_ids: list[int] | None = None
    reception_note: str | None = None

    # Accepted only so they can be refused with a useful message.
    room_id: str | None = None
    date: date_type | None = None
    entry: str | None = None
    exit: str | None = None


class AttendeeResponseIn(BaseModel):
    """POST /api/bookings/{id}/response."""

    response: Literal["ACCEPTED", "DECLINED", "PENDING"]


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
        description="Whether the requesting user may change the details."
    )
    can_respond: bool = Field(
        default=False,
        description="Whether the requesting user is an attendee who may reply.",
    )

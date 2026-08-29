"""Availability payloads.

Every time in here is an ``"HH:MM"`` string in Asia/Kolkata. The frontend never
receives a UTC timestamp and never does timezone arithmetic — conversion happens
server-side in :mod:`app.core.time`.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

from pydantic import BaseModel, Field

ClosedReason = Literal["past", "weekly_closure", "holiday"]


class BookingOnGrid(BaseModel):
    """One booked block as the grid draws it."""

    id: str
    entry: str = Field(description='Local start, "13:00".', examples=["13:00"])
    exit: str = Field(description='Local end, "15:00".', examples=["15:00"])
    title: str
    host: str = Field(description="Full name of whoever is conducting it.")
    department: str
    booked_by: str = Field(description="Full name of whoever filled in the form.")
    booked_by_id: int = Field(
        description="So the frontend can mark a booking as the viewer's own."
    )


class RoomAvailability(BaseModel):
    """One room's row of the grid for one day."""

    id: str
    name: str
    display_order: int
    colour_var: str
    min_people: int
    free_slot_count: int = Field(
        description=(
            "Bookable slot starts left today. Slots inside an existing booking "
            "and slots already gone do not count."
        )
    )
    bookings: list[BookingOnGrid]


class AvailabilityResponse(BaseModel):
    """Everything needed to paint the grid for one date, in one call."""

    date: date_type
    closed: bool
    closed_reason: ClosedReason | None = None
    closed_detail: str | None = Field(
        default=None,
        description='Holiday name when closed_reason is "holiday", else null.',
    )
    open_time: str = Field(examples=["09:00"])
    close_time: str = Field(examples=["20:00"])
    slot_minutes: int
    min_booking_minutes: int
    max_booking_minutes: int
    max_advance_days: int
    last_bookable_date: date_type = Field(
        description="The furthest date that may be booked, inclusive."
    )
    closed_weekdays: list[int] = Field(
        description="Monday=0 ... Sunday=6. Days the branch is shut every week."
    )
    timezone: str
    slots: list[str] = Field(
        description='Every bookable start time, "09:00" through "19:30".'
    )
    rooms: list[RoomAvailability]


class ExitCapResponse(BaseModel):
    """The latest exit permitted for a given entry — the prototype's capAfter()."""

    room: str
    date: date_type
    entry: str
    exit_cap: str = Field(
        description="min(next booking start, entry + 4h, closing time)."
    )
    next_booking_entry: str | None = Field(
        default=None,
        description="Start of the next booking for this room, if one caps it.",
    )
    limited_by: Literal["next_booking", "max_length", "closing_time"]
    options: list[str] = Field(
        description="Every legal exit time for this entry, in 30-minute steps."
    )

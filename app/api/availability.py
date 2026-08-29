"""Availability endpoints.

``GET /api/availability`` is the call that paints the entire grid: one request
per day change, never one per room. ``GET /api/availability/exit-cap`` is the
prototype's ``capAfter()`` moved server-side, so step 4 of the wizard can offer
exactly the exits the database will accept.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.core.auth import CurrentUser
from app.core.errors import NotFoundError, ValidationError
from app.database import get_db
from app.models import Room
from app.schemas.availability import (
    AvailabilityResponse,
    BookingOnGrid,
    ExitCapResponse,
    RoomAvailability,
)
from app.services import availability as av

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


def _parse_time(value: str) -> int:
    """Parse an HH:MM query parameter, or refuse it with our own message."""
    try:
        return timeutil.parse_hhmm(value)
    except ValueError as exc:
        raise ValidationError(messages.BAD_TIME) from exc


@router.get(
    "/availability",
    response_model=AvailabilityResponse,
    summary="Everything needed to paint the grid for one date",
)
def get_availability(
    db: DbSession,
    actor: CurrentUser,
    date: Annotated[date_type, Query(description="Local date, YYYY-MM-DD.")],
) -> AvailabilityResponse:
    """Per-room bookings and free slot counts for a single local date.

    Availability is per room: a window booked for Power says nothing about Pulse.
    A closed day still returns every room, so the grid can render itself greyed
    out rather than empty.
    """
    status = av.day_status(db, date)
    day_rooms = av.day_by_room(db, date)

    rooms: list[RoomAvailability] = []
    for room_day in day_rooms:
        windows = room_day.windows
        rooms.append(
            RoomAvailability(
                id=room_day.room.id,
                name=room_day.room.name,
                display_order=room_day.room.display_order,
                colour_var=room_day.room.colour_var,
                min_people=room_day.room.min_people,
                free_slot_count=(
                    0 if status.closed else av.free_slot_count(windows, date)
                ),
                bookings=[
                    BookingOnGrid(
                        id=str(booking.id),
                        entry=timeutil.to_hhmm(
                            timeutil.minutes_from_midnight(booking.entry_time)
                        ),
                        exit=timeutil.to_hhmm(
                            timeutil.minutes_from_midnight(booking.exit_time)
                        ),
                        title=booking.title,
                        host=booking.conductor.full_name,
                        department=booking.department.name,
                        booked_by=booking.booker.full_name,
                        booked_by_id=booking.booked_by,
                    )
                    for booking in room_day.bookings
                ],
            )
        )

    return AvailabilityResponse(
        date=date,
        closed=status.closed,
        closed_reason=status.reason,
        closed_detail=status.holiday_name,
        open_time=timeutil.to_hhmm(settings.open_minutes),
        close_time=timeutil.to_hhmm(settings.close_minutes),
        slot_minutes=settings.slot_minutes,
        min_booking_minutes=settings.min_booking_minutes,
        max_booking_minutes=settings.max_booking_minutes,
        max_advance_days=settings.max_advance_days,
        last_bookable_date=av.last_bookable_date(),
        closed_weekdays=list(settings.closed_weekdays),
        timezone=settings.tz,
        slots=[timeutil.to_hhmm(m) for m in av.slot_starts()],
        rooms=rooms,
    )


@router.get(
    "/availability/exit-cap",
    response_model=ExitCapResponse,
    summary="Latest permitted exit for a given entry",
)
def get_exit_cap(
    db: DbSession,
    actor: CurrentUser,
    room: Annotated[str, Query(description="Room slug, e.g. power.")],
    date: Annotated[date_type, Query(description="Local date, YYYY-MM-DD.")],
    entry: Annotated[str, Query(description='Local start, "12:30".')],
) -> ExitCapResponse:
    """``min(next booking start, entry + 4 hours, closing time)``.

    Section 4's worked example: Power free from 12:00 but booked from 13:00, an
    entry of 12:30 allows an exit of 13:00 only.
    """
    room_row = db.get(Room, room)
    if room_row is None:
        raise NotFoundError(messages.UNKNOWN_ROOM.format(room=room))

    entry_minutes = _parse_time(entry)

    if entry_minutes < settings.open_minutes or entry_minutes > (
        settings.close_minutes - settings.min_booking_minutes
    ):
        raise ValidationError(messages.outside_hours())

    if not av.is_on_slot_boundary(entry_minutes):
        raise ValidationError(messages.NOT_ON_SLOT_BOUNDARY)

    windows = [
        av.Window(
            timeutil.minutes_from_midnight(b.entry_time),
            timeutil.minutes_from_midnight(b.exit_time),
        )
        for b in av.confirmed_bookings(db, date, room)
    ]

    if av.is_blocked_at(windows, entry_minutes):
        raise ValidationError(
            messages.ENTRY_ALREADY_BOOKED.format(
                room=room_row.name, entry=timeutil.format_clock(entry_minutes)
            )
        )

    cap = av.exit_cap(windows, entry_minutes)
    following = av.next_booking_start(windows, entry_minutes)

    if following is not None and cap == following:
        limited_by = "next_booking"
    elif cap == entry_minutes + settings.max_booking_minutes:
        limited_by = "max_length"
    else:
        limited_by = "closing_time"

    options = [
        timeutil.to_hhmm(m)
        for m in range(
            entry_minutes + settings.min_booking_minutes,
            cap + 1,
            settings.slot_minutes,
        )
    ]

    return ExitCapResponse(
        room=room,
        date=date,
        entry=timeutil.to_hhmm(entry_minutes),
        exit_cap=timeutil.to_hhmm(cap),
        next_booking_entry=(
            timeutil.to_hhmm(following) if following is not None else None
        ),
        limited_by=limited_by,
        options=options,
    )

"""Booking endpoints.

Phase 2 creates and reads. Cancel, edit, no-show and attendee responses arrive in
Phase 3.

The acting user always comes from :data:`app.core.auth.CurrentUser`, never from
the request body — section 9 decides what somebody may do by comparing them
against ``booked_by`` and ``conducted_by``, and a body field would let a caller
claim to be somebody else.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import messages
from app.core import time as timeutil
from app.core.auth import CurrentUser
from app.core.errors import NotFoundError, ValidationError
from app.database import get_db
from app.models import Booking
from app.schemas.bookings import AttendeeOut, BookingCreate, BookingDetail
from app.services import permissions
from app.services.booking import BookingRequest, create_booking

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


def _parse_time(value: str) -> int:
    try:
        return timeutil.parse_hhmm(value)
    except ValueError as exc:
        raise ValidationError(messages.BAD_TIME) from exc


def _load_booking(db: Session, booking_id: str) -> Booking:
    """Fetch a booking by id, refusing a malformed or unknown id politely."""
    try:
        key = uuid.UUID(booking_id)
    except ValueError as exc:
        raise NotFoundError(messages.UNKNOWN_BOOKING) from exc

    booking = db.scalar(
        select(Booking)
        .where(Booking.id == key)
        .options(
            selectinload(Booking.room),
            selectinload(Booking.department),
            selectinload(Booking.conductor),
            selectinload(Booking.booker),
            selectinload(Booking.attendees),
        )
    )
    if booking is None:
        raise NotFoundError(messages.UNKNOWN_BOOKING)
    return booking


def to_detail(booking: Booking, actor) -> BookingDetail:
    """Shape a booking for the detail panel, including this viewer's rights."""
    return BookingDetail(
        id=str(booking.id),
        room_id=booking.room_id,
        room_name=booking.room.name,
        colour_var=booking.room.colour_var,
        date=booking.booking_date,
        entry=timeutil.to_hhmm(timeutil.minutes_from_midnight(booking.entry_time)),
        exit=timeutil.to_hhmm(timeutil.minutes_from_midnight(booking.exit_time)),
        title=booking.title,
        department_id=booking.department_id,
        department=booking.department.name,
        conducted_by_id=booking.conducted_by,
        host=booking.conductor.full_name,
        booked_by_id=booking.booked_by,
        booked_by=booking.booker.full_name,
        reception_note=booking.reception_note,
        status=booking.status.value,
        attendees=[
            AttendeeOut(
                id=attendee.user.id,
                full_name=attendee.user.full_name,
                email=attendee.user.email,
                response_status=attendee.response_status.value,
            )
            for attendee in booking.attendees
        ],
        can_cancel=permissions.can_cancel(actor, booking),
        can_edit=permissions.can_edit(actor, booking),
    )


@router.post(
    "/bookings",
    response_model=BookingDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a booking",
    responses={
        409: {"description": "The room was taken. Names the rooms still free."}
    },
)
def create(
    payload: BookingCreate,
    actor: CurrentUser,
    db: DbSession,
    response: Response,
) -> BookingDetail:
    """Validate, re-check inside the transaction, insert, let the database rule.

    A 409 carries the section 5 message naming this room and the rooms still
    free, or section 10's message when nothing is free at all.
    """
    request = BookingRequest(
        room_id=payload.room_id,
        day=payload.date,
        entry=_parse_time(payload.entry),
        exit=_parse_time(payload.exit),
        department_id=payload.department_id,
        conducted_by=payload.conducted_by,
        attendee_ids=payload.attendee_ids,
        title=payload.title,
        reception_note=payload.reception_note,
    )

    booking = create_booking(db, actor, request)
    db.commit()

    fresh = _load_booking(db, str(booking.id))
    response.headers["Location"] = f"/api/bookings/{fresh.id}"
    return to_detail(fresh, actor)


@router.get(
    "/bookings/{booking_id}",
    response_model=BookingDetail,
    summary="Booking detail panel payload",
)
def detail(booking_id: str, actor: CurrentUser, db: DbSession) -> BookingDetail:
    return to_detail(_load_booking(db, booking_id), actor)

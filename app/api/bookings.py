"""Booking endpoints - create, read, cancel, edit, no-show and attendee response.

The acting user always comes from :data:`app.core.auth.CurrentUser`, never from
the request body. Section 9 decides what somebody may do by comparing them
against ``booked_by`` and ``conducted_by``, and a body field would let a caller
claim to be somebody else.

Every one of these commits its own transaction, so the audit row and the
notification log rows land with the change they describe or not at all.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import messages
from app.core import time as timeutil
from app.core.auth import Actor
from app.core.errors import NotFoundError, ValidationError
from app.database import get_db
from app.models import AttendeeResponse, Booking, BookingAttendee, NotificationEvent, User
from app.schemas.bookings import (
    AttendeeOut,
    AttendeeResponseIn,
    BookingCreate,
    BookingDetail,
    BookingUpdate,
)
from app.services import audit, lifecycle, notifications, permissions
from app.services.booking import BookingRequest, create_booking
from app.services.lifecycle import BookingEdit

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


def _parse_time(value: str) -> int:
    try:
        return timeutil.parse_hhmm(value)
    except ValueError as exc:
        raise ValidationError(messages.BAD_TIME) from exc


def _load_host(db: Session, user_id: int | None) -> User:
    """Without a session, the host named in the form is the acting user.

    create_booking validates them properly; this only has to fail politely when
    the field is missing, because there is no session to fall back on.
    """
    if user_id is None:
        raise ValidationError(messages.CONDUCTOR_REQUIRED)
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise ValidationError(messages.CONDUCTOR_REQUIRED)
    return user


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
            selectinload(Booking.attendees).selectinload(BookingAttendee.user),
        )
    )
    if booking is None:
        raise NotFoundError(messages.UNKNOWN_BOOKING)
    return booking


def to_detail(booking: Booking, actor: User | None) -> BookingDetail:
    """Shape a booking for the detail panel, including this viewer's rights.

    With no sign-in there is nobody to compare against, so everything is
    offered. Switch SIGN_IN_REQUIRED on and the ownership rules apply again.
    """
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
        can_cancel=actor is None or permissions.can_cancel(actor, booking),
        can_edit=actor is None or permissions.can_edit(actor, booking),
        can_respond=actor is not None and permissions.is_attendee(actor, booking),
    )


# ------------------------------------------------------------------ create


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
    actor: Actor,
    db: DbSession,
    response: Response,
) -> BookingDetail:
    """Validate, re-check inside the transaction, insert, let the database rule.

    On success every recipient in section 8 is notified BOOKED, and an audit row
    is written. A 409 carries the section 5 message naming this room and the
    rooms still free.
    """
    # With no sign-in, the booking belongs to whoever the form named as host.
    booker = actor or _load_host(db, payload.conducted_by)

    # The host is whoever is signed in. The form stopped asking - the person at
    # the keyboard is the person the room is for - so an omitted conducted_by
    # means "me". It is still accepted when sent, because an edit sends it and
    # because booking on somebody else's behalf is a change of one field, not a
    # change of design. What it is not is a way to claim to be somebody else:
    # `booker` comes from the session either way, and section 9 compares
    # against that.
    conducted_by = payload.conducted_by if payload.conducted_by is not None else booker.id

    request = BookingRequest(
        room_id=payload.room_id,
        day=payload.date,
        entry=_parse_time(payload.entry),
        exit=_parse_time(payload.exit),
        department_id=payload.department_id,
        conducted_by=conducted_by,
        attendee_ids=payload.attendee_ids,
        title=payload.title,
        reception_note=payload.reception_note,
    )

    booking = create_booking(db, booker, request)

    audit.record(
        db,
        action=audit.CREATED,
        booking=booking,
        actor=booker,
        after=audit.snapshot(booking),
    )
    notifications.notify(db, booking, NotificationEvent.BOOKED)
    db.commit()

    fresh = _load_booking(db, str(booking.id))
    response.headers["Location"] = f"/api/bookings/{fresh.id}"
    return to_detail(fresh, actor)


@router.get(
    "/bookings/{booking_id}",
    response_model=BookingDetail,
    summary="Booking detail panel payload",
)
def detail(booking_id: str, actor: Actor, db: DbSession) -> BookingDetail:
    return to_detail(_load_booking(db, booking_id), actor)


# ------------------------------------------------------------------ cancel


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=BookingDetail,
    summary="Cancel a booking",
)
def cancel(booking_id: str, actor: Actor, db: DbSession) -> BookingDetail:
    """Free the room immediately and tell all five recipient groups.

    The booking is marked CANCELLED and kept for the audit record; it is never
    deleted. Cancelling a meeting that is already running is allowed and releases
    the remaining time.
    """
    booking = _load_booking(db, booking_id)
    lifecycle.cancel_booking(db, actor, booking)
    db.commit()
    return to_detail(_load_booking(db, booking_id), actor)


# -------------------------------------------------------------------- edit


@router.patch(
    "/bookings/{booking_id}",
    response_model=BookingDetail,
    summary="Change a booking's details",
)
def update(
    booking_id: str,
    payload: BookingUpdate,
    actor: Actor,
    db: DbSession,
) -> BookingDetail:
    """Details only - title, department, conductor, attendees, reception note.

    Room, date and time are never editable; sending any of them is refused with
    the instruction to cancel and re-book, so that the exclusion constraint gets
    to adjudicate the new window.
    """
    provided = payload.model_fields_set

    if provided & {"room_id", "date", "entry", "exit"}:
        raise ValidationError(messages.IMMUTABLE_FIELDS)

    booking = _load_booking(db, booking_id)

    edit = BookingEdit(
        title=payload.title,
        department_id=payload.department_id,
        conducted_by=payload.conducted_by,
        attendee_ids=payload.attendee_ids,
        reception_note=payload.reception_note,
        reception_note_set="reception_note" in provided,
    )

    lifecycle.edit_booking(db, actor, booking, edit)
    db.commit()
    return to_detail(_load_booking(db, booking_id), actor)


# ---------------------------------------------------------------- response


@router.post(
    "/bookings/{booking_id}/response",
    response_model=BookingDetail,
    summary="Accept or decline your own invitation",
)
def respond(
    booking_id: str,
    payload: AttendeeResponseIn,
    actor: Actor,
    db: DbSession,
) -> BookingDetail:
    """An attendee's own reply - record-keeping only.

    Section 9: an attendee cannot cancel, only decline. No notification fires and
    nothing on the grid changes.
    """
    booking = _load_booking(db, booking_id)
    lifecycle.set_attendee_response(
        db, actor, booking, AttendeeResponse(payload.response)
    )
    db.commit()
    return to_detail(_load_booking(db, booking_id), actor)

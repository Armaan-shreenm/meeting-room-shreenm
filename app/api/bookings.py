"""Booking endpoints — create, read, cancel, edit, no-show and attendee response.

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
from app.core.auth import CurrentUser
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


def to_detail(booking: Booking, actor: User) -> BookingDetail:
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
        can_mark_no_show=(
            permissions.can_mark_no_show(actor)
            and booking.status.value == "CONFIRMED"
        ),
        can_respond=permissions.is_attendee(actor, booking),
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
    actor: CurrentUser,
    db: DbSession,
    response: Response,
) -> BookingDetail:
    """Validate, re-check inside the transaction, insert, let the database rule.

    On success every recipient in section 8 is notified BOOKED, and an audit row
    is written. A 409 carries the section 5 message naming this room and the
    rooms still free.
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

    audit.record(
        db,
        action=audit.CREATED,
        booking=booking,
        actor=actor,
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
def detail(booking_id: str, actor: CurrentUser, db: DbSession) -> BookingDetail:
    return to_detail(_load_booking(db, booking_id), actor)


# ------------------------------------------------------------------ cancel


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=BookingDetail,
    summary="Cancel a booking",
)
def cancel(booking_id: str, actor: CurrentUser, db: DbSession) -> BookingDetail:
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
    actor: CurrentUser,
    db: DbSession,
) -> BookingDetail:
    """Details only — title, department, conductor, attendees, reception note.

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


# ------------------------------------------------------------------ no-show


@router.post(
    "/bookings/{booking_id}/no-show",
    response_model=BookingDetail,
    summary="Release an unused room as a no-show",
)
def no_show(booking_id: str, actor: CurrentUser, db: DbSession) -> BookingDetail:
    """Reception or admin, no earlier than 15 minutes after the start (D-06).

    Frees the room immediately, is audit-logged, and sends no notification.
    """
    booking = _load_booking(db, booking_id)
    lifecycle.mark_no_show(db, actor, booking)
    db.commit()
    return to_detail(_load_booking(db, booking_id), actor)


@router.post(
    "/bookings/{booking_id}/restore",
    response_model=BookingDetail,
    summary="Undo a no-show, if the window is still free",
)
def restore(booking_id: str, actor: CurrentUser, db: DbSession) -> BookingDetail:
    """Put a released booking back to CONFIRMED.

    Whether the window is still free is decided by the exclusion constraint, not
    by a pre-check: the room was released, so somebody may have taken it.
    """
    booking = _load_booking(db, booking_id)
    lifecycle.restore_booking(db, actor, booking)
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
    actor: CurrentUser,
    db: DbSession,
) -> BookingDetail:
    """An attendee's own reply — record-keeping only.

    Section 9: an attendee cannot cancel, only decline. No notification fires and
    nothing on the grid changes.
    """
    booking = _load_booking(db, booking_id)
    lifecycle.set_attendee_response(
        db, actor, booking, AttendeeResponse(payload.response)
    )
    db.commit()
    return to_detail(_load_booking(db, booking_id), actor)

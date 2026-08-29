"""Booking creation — spec sections 3, 5, 7 and 10.

Section 7 requires three layers against a double booking and says only the third
is a guarantee:

1. **Screen** — the frontend greys out booked windows. Not this module's job.
2. **Server** — :func:`create_booking` re-checks availability inside the same
   transaction as the insert, immediately before it.
3. **Database** — the ``no_double_booking`` EXCLUDE constraint. Two requests can
   pass layer 2 simultaneously; only this stops them both writing.

Layer 3 is caught by SQLSTATE ``23P01`` (exclusion_violation), never by matching
text in an error message and never by a bare ``except``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date

from psycopg2 import errorcodes
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Booking,
    BookingAttendee,
    BookingStatus,
    Department,
    Room,
    User,
)
from app.services import availability as av
from app.services.availability import Window

logger = logging.getLogger(__name__)


@dataclass
class BookingRequest:
    """A validated-on-arrival booking request in local terms."""

    room_id: str
    day: date
    entry: int  # local minutes from midnight
    exit: int
    department_id: int
    conducted_by: int
    attendee_ids: list[int]
    title: str
    reception_note: str | None


# --------------------------------------------------------------- validation


def _validate_day(db: Session, day: date) -> None:
    """Section 10's date rules, in the order the table lists them."""
    if day < timeutil.local_today():
        raise ValidationError(messages.DATE_PAST)

    status = av.day_status(db, day)
    if status.closed:
        # Sunday and declared holidays share one message by design.
        raise ValidationError(messages.OFFICE_CLOSED)

    if av.is_too_far_ahead(day):
        raise ValidationError(
            messages.TOO_FAR_AHEAD.format(days=settings.max_advance_days)
        )


def _validate_window(entry: int, exit_: int) -> None:
    """Section 10's time rules, in the order the table lists them."""
    if exit_ <= entry:
        raise ValidationError(messages.EXIT_NOT_AFTER_ENTRY)

    length = exit_ - entry
    if length < settings.min_booking_minutes:
        raise ValidationError(messages.TOO_SHORT)
    if length > settings.max_booking_minutes:
        raise ValidationError(messages.TOO_LONG)

    if entry < settings.open_minutes or exit_ > settings.close_minutes:
        raise ValidationError(messages.OUTSIDE_HOURS)

    if not av.is_on_slot_boundary(entry) or not av.is_on_slot_boundary(exit_):
        raise ValidationError(messages.NOT_ON_SLOT_BOUNDARY)


def _validate_not_past_today(day: date, entry: int) -> None:
    """A slot earlier today has gone, even though the date itself has not."""
    if day == timeutil.local_today() and entry < timeutil.now_local_minutes():
        raise ValidationError(messages.TIME_ALREADY_PASSED)


def _load_room(db: Session, room_id: str) -> Room:
    room = db.get(Room, room_id)
    if room is None:
        raise NotFoundError(messages.UNKNOWN_ROOM.format(room=room_id))
    return room


def _load_department(db: Session, department_id: int | None) -> Department:
    if department_id is None:
        raise ValidationError(messages.DEPARTMENT_REQUIRED)
    department = db.get(Department, department_id)
    if department is None:
        raise ValidationError(messages.UNKNOWN_DEPARTMENT)
    return department


def _load_conductor(db: Session, user_id: int | None) -> User:
    """D-04: only an active directory member may conduct a meeting."""
    if user_id is None:
        raise ValidationError(messages.CONDUCTOR_REQUIRED)
    user = db.get(User, user_id)
    if user is None:
        raise ValidationError(messages.CONDUCTOR_REQUIRED)
    if not user.is_active:
        raise ValidationError(
            messages.CONDUCTOR_NOT_IN_DIRECTORY.format(name=user.full_name)
        )
    return user


def _load_attendees(db: Session, attendee_ids: list[int]) -> list[User]:
    """Attendees are optional (field 7) but must be real directory members."""
    unique_ids = list(dict.fromkeys(attendee_ids))
    if not unique_ids:
        return []

    found = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(unique_ids))).all()
    }

    attendees: list[User] = []
    for user_id in unique_ids:
        user = found.get(user_id)
        if user is None:
            raise ValidationError(
                messages.ATTENDEE_NOT_IN_DIRECTORY.format(name=f"User {user_id}")
            )
        if not user.is_active:
            raise ValidationError(
                messages.ATTENDEE_NOT_IN_DIRECTORY.format(name=user.full_name)
            )
        attendees.append(user)
    return attendees


# ------------------------------------------------------------- clash report


def build_conflict(db: Session, room: Room, day: date, window: Window) -> ConflictError:
    """Turn a lost race into the message section 5 and section 10 require.

    The free-room list is computed from live availability at this instant, so it
    reflects whatever the winner just did.
    """
    entry_text = timeutil.format_clock(window.entry)
    exit_text = timeutil.format_clock(window.exit)

    free = av.free_rooms_for_window(db, day, window, exclude_room_id=room.id)

    if free:
        return ConflictError(
            messages.room_taken(
                room.name, [r.name for r in free], entry_text, exit_text
            ),
            free_rooms=[r.name for r in free],
        )

    # Nothing free for that window at all — section 10's other message.
    total_rooms = len(av.rooms_in_order(db))
    duration = window.exit - window.entry
    first = av.first_free_room(db, day, duration, not_before=window.entry)

    return ConflictError(
        messages.no_room_free(
            room_count=total_rooms,
            entry=entry_text,
            exit_=exit_text,
            first_room=first[0].name if first else None,
            first_time=timeutil.format_clock(first[1]) if first else None,
        ),
        free_rooms=[],
    )


# ------------------------------------------------------------------ create


def create_booking(db: Session, actor: User, request: BookingRequest) -> Booking:
    """Validate, re-check, insert. Raises on every refusal with a real message.

    The caller owns the transaction. On success the booking is flushed but not
    committed, so an endpoint can add related rows before committing.
    """
    room = _load_room(db, request.room_id)

    _validate_day(db, request.day)
    _validate_window(request.entry, request.exit)
    _validate_not_past_today(request.day, request.entry)

    department = _load_department(db, request.department_id)
    conductor = _load_conductor(db, request.conducted_by)
    attendees = _load_attendees(db, request.attendee_ids)

    window = Window(request.entry, request.exit)

    room_windows = [
        Window(
            timeutil.minutes_from_midnight(b.entry_time),
            timeutil.minutes_from_midnight(b.exit_time),
        )
        for b in av.confirmed_bookings(db, request.day, room.id)
    ]

    # ---- Layer 2: re-check inside this transaction, immediately before insert.
    #
    # Every kind of overlap lands here, including an exit that runs into the next
    # booking (section 4). They are all the same thing to the specification — a
    # clash — and section 5 gives one wording for it, naming the room and the
    # rooms still free. Refusing them separately would invent a message the
    # section 10 table does not have.
    if not av.is_window_free(room_windows, window):
        raise build_conflict(db, room, request.day, window)

    entry_time = timeutil.local_datetime(request.day, request.entry)
    exit_time = timeutil.local_datetime(request.day, request.exit)

    booking = Booking(
        id=uuid.uuid4(),
        room_id=room.id,
        booking_date=timeutil.booking_date_for(entry_time),
        entry_time=entry_time,
        exit_time=exit_time,
        department_id=department.id,
        conducted_by=conductor.id,
        booked_by=actor.id,
        title=(request.title or "").strip() or settings.default_meeting_title,
        reception_note=(request.reception_note or "").strip() or None,
        status=BookingStatus.CONFIRMED,
    )
    db.add(booking)

    for attendee in attendees:
        db.add(BookingAttendee(booking_id=booking.id, user_id=attendee.id))

    # ---- Layer 3: the database is the only real guarantee.
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        pgcode = getattr(exc.orig, "pgcode", None)
        if pgcode == errorcodes.EXCLUSION_VIOLATION:
            logger.info(
                "Layer 3 refused a double booking: room=%s %s-%s on %s",
                room.id,
                request.entry,
                request.exit,
                request.day,
            )
            raise build_conflict(db, room, request.day, window) from exc
        raise

    logger.info(
        "Booked %s %s-%s on %s for %s",
        room.id,
        timeutil.to_hhmm(request.entry),
        timeutil.to_hhmm(request.exit),
        request.day,
        actor.email,
    )
    return booking

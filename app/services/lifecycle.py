"""What happens to a booking after it exists — spec sections 8, 9 and 11.

Cancel, edit the details, release a no-show, restore one, and record an
attendee's own response. Creation lives in :mod:`app.services.booking`.

Two rules run through all of it:

* **A booking is never deleted.** Cancelling sets ``CANCELLED``; a no-show sets
  ``NO_SHOW``. Both release the room instantly, because the ``no_double_booking``
  exclusion constraint is scoped to ``CONFIRMED`` rows only. The row stays for
  the audit record.
* **Room, date and time are never editable.** Changing when or where a meeting
  happens is a cancellation and a fresh booking, so that the exclusion
  constraint re-adjudicates it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.core.errors import PermissionError_, ValidationError
from app.models import (
    AttendeeResponse,
    Booking,
    BookingAttendee,
    BookingStatus,
    Department,
    NotificationEvent,
    User,
)
from app.services import audit, notifications, permissions

logger = logging.getLogger(__name__)


@dataclass
class BookingEdit:
    """The details that may change. Room, date and time are absent by design."""

    title: str | None = None
    department_id: int | None = None
    conducted_by: int | None = None
    attendee_ids: list[int] | None = None
    reception_note: str | None = None
    # Distinguishes "leave the note alone" from "clear the note".
    reception_note_set: bool = False


# ----------------------------------------------------------------- cancel


def cancel_booking(db: Session, actor: User, booking: Booking) -> Booking:
    """Cancel a booking — spec section 9.

    Permitted for the booker, the conductor, reception and admin. An attendee is
    refused: they may only decline their own place.

    Cancelling a meeting that has already started is allowed and releases the
    remaining time. Nothing is truncated: the whole window becomes bookable
    again, and the part that has already gone is unbookable anyway because a
    booking cannot start in the past.
    """
    if not permissions.can_cancel(actor, booking):
        raise PermissionError_(
            messages.CANNOT_CANCEL.format(owner=booking.booker.full_name)
        )

    if booking.status == BookingStatus.CANCELLED:
        raise ValidationError(messages.ALREADY_CANCELLED)

    before = audit.snapshot(booking)
    booking.status = BookingStatus.CANCELLED
    db.flush()

    audit.record(
        db,
        action=audit.CANCELLED,
        booking=booking,
        actor=actor,
        before=before,
        after=audit.snapshot(booking),
    )

    # Section 8: a cancellation notice goes to all five recipient groups.
    notifications.notify(db, booking, NotificationEvent.CANCELLED)

    logger.info("Cancelled booking %s by %s", booking.id, actor.email)
    return booking


# ------------------------------------------------------------------- edit


def edit_booking(db: Session, actor: User, booking: Booking, edit: BookingEdit) -> Booking:
    """Change the details of a booking — never its room, date or time.

    Fires CHANGED. Attendee churn is reported per person: someone newly added is
    told BOOKED, someone removed is told CANCELLED, everyone else CHANGED.
    """
    if not permissions.can_edit(actor, booking):
        raise PermissionError_(
            messages.CANNOT_EDIT.format(owner=booking.booker.full_name)
        )

    if booking.status == BookingStatus.CANCELLED:
        raise ValidationError(messages.CANNOT_CHANGE_CANCELLED)

    before = audit.snapshot(booking)

    if edit.title is not None:
        booking.title = edit.title.strip() or settings.default_meeting_title

    if edit.department_id is not None:
        department = db.get(Department, edit.department_id)
        if department is None:
            raise ValidationError(messages.UNKNOWN_DEPARTMENT)
        booking.department_id = department.id

    if edit.conducted_by is not None:
        conductor = db.get(User, edit.conducted_by)
        if conductor is None:
            raise ValidationError(messages.CONDUCTOR_REQUIRED)
        if not conductor.is_active:
            raise ValidationError(
                messages.CONDUCTOR_NOT_IN_DIRECTORY.format(name=conductor.full_name)
            )
        booking.conducted_by = conductor.id

    if edit.reception_note_set:
        note = (edit.reception_note or "").strip()
        booking.reception_note = note or None

    added: list[User] = []
    removed: list[User] = []

    if edit.attendee_ids is not None:
        added, removed = _apply_attendees(db, booking, edit.attendee_ids)

    db.flush()
    db.refresh(booking)

    audit.record(
        db,
        action=audit.UPDATED,
        booking=booking,
        actor=actor,
        before=before,
        after=audit.snapshot(booking),
    )

    if added or removed:
        notifications.notify_attendee_change(db, booking, added, removed)
    else:
        notifications.notify(db, booking, NotificationEvent.CHANGED)

    logger.info("Edited booking %s by %s", booking.id, actor.email)
    return booking


def _apply_attendees(
    db: Session, booking: Booking, wanted_ids: list[int]
) -> tuple[list[User], list[User]]:
    """Reconcile the attendee list, returning (added, removed) users."""
    wanted = list(dict.fromkeys(wanted_ids))

    found = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(wanted))).all()
    } if wanted else {}

    for user_id in wanted:
        user = found.get(user_id)
        if user is None or not user.is_active:
            name = user.full_name if user else f"User {user_id}"
            raise ValidationError(
                messages.ATTENDEE_NOT_IN_DIRECTORY.format(name=name)
            )

    current = {a.user_id: a for a in booking.attendees}

    added_ids = [uid for uid in wanted if uid not in current]
    removed_ids = [uid for uid in current if uid not in wanted]

    removed_users = [
        current[uid].user for uid in removed_ids
    ]
    for uid in removed_ids:
        db.delete(current[uid])

    for uid in added_ids:
        db.add(BookingAttendee(booking_id=booking.id, user_id=uid))

    db.flush()
    return [found[uid] for uid in added_ids], removed_users


# ---------------------------------------------------------------- no-show
#
# Removed. D-06 released an unused room after fifteen minutes, but only
# reception or an administrator could do it, and privilege levels no longer
# exist. Reinstating it means restoring a role in app.services.permissions and
# the two endpoints in app.api.bookings. BookingStatus.NO_SHOW is still in the
# schema, so no data migration is needed to bring it back.

# -------------------------------------------------------- attendee response


def set_attendee_response(
    db: Session, actor: User, booking: Booking, response: AttendeeResponse
) -> BookingAttendee:
    """An attendee accepting or declining their own place — spec section 9.

    Record-keeping only: no notification fires and nothing on the grid changes.
    An attendee can only ever change their own row.
    """
    row = next(
        (a for a in booking.attendees if a.user_id == actor.id),
        None,
    )
    if row is None:
        raise PermissionError_(messages.NOT_AN_ATTENDEE)

    if booking.status == BookingStatus.CANCELLED:
        raise ValidationError(messages.ALREADY_CANCELLED)

    before = audit.snapshot(booking)
    row.response_status = response
    db.flush()

    audit.record(
        db,
        action=audit.RESPONDED,
        booking=booking,
        actor=actor,
        before=before,
        after=audit.snapshot(booking),
    )

    logger.info(
        "Attendee %s responded %s to booking %s",
        actor.email,
        response.value,
        booking.id,
    )
    return row

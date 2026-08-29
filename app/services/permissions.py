"""Who may do what.

**There are no privilege levels.** Whoever books a room is the only person who
can cancel or change it. Reception cannot, an administrator cannot, an attendee
cannot. The one rule the business asked for is that nobody can cancel somebody
else's booking, and that is the whole of it.

``users.role`` still exists in the database and is still returned by the API,
but nothing in this module reads it. It is left in place so that reinstating a
front-desk role later is a change here rather than a migration. If you are
reading this because you need reception to release a room, that is the file to
change — and the endpoints that used to do it were removed, not disabled.
"""

from __future__ import annotations

from app.models import Booking, User


def is_owner(actor: User, booking: Booking) -> bool:
    """Did this person make the booking?"""
    return actor.id == booking.booked_by


def can_cancel(actor: User, booking: Booking) -> bool:
    """Only the person who booked it."""
    return is_owner(actor, booking)


def can_edit(actor: User, booking: Booking) -> bool:
    """Details only, and only for the person who booked it.

    The room, the date and the time are never editable by anybody: changing
    when or where a meeting happens is a cancellation and a fresh booking, so
    that the exclusion constraint gets to adjudicate the new window.
    """
    return is_owner(actor, booking)


def is_attendee(actor: User, booking: Booking) -> bool:
    """On the attendee list, which is what lets them accept or decline."""
    return any(attendee.user_id == actor.id for attendee in booking.attendees)

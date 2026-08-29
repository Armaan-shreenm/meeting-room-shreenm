"""Who may do what — spec section 9.

| Role                    | Can cancel        |
| ----------------------- | ----------------- |
| The person who booked it| Their own bookings|
| The person conducting it| That meeting      |
| Reception               | Any booking       |
| Admin                   | Any booking       |
| An attendee             | No — decline only |

Editing the details follows exactly the same list. An attendee may only change
their own ``response_status``, never cancel and never edit.

These predicates are the single source of truth. The frontend uses the flags on
the booking detail to hide what a viewer cannot do; the server calls the same
functions to refuse it regardless of what the frontend showed.
"""

from __future__ import annotations

from app.models import Booking, User, UserRole

# Reception and admin act on any booking at the branch.
PRIVILEGED_ROLES = frozenset({UserRole.RECEPTION, UserRole.ADMIN})


def is_privileged(actor: User) -> bool:
    """Reception or admin."""
    return actor.role in PRIVILEGED_ROLES


def is_owner(actor: User, booking: Booking) -> bool:
    """Booked it, or is running it."""
    return actor.id in (booking.booked_by, booking.conducted_by)


def can_cancel(actor: User, booking: Booking) -> bool:
    """Spec section 9."""
    return is_privileged(actor) or is_owner(actor, booking)


def can_edit(actor: User, booking: Booking) -> bool:
    """Details only — room, date and time are never editable by anyone."""
    return can_cancel(actor, booking)


def can_mark_no_show(actor: User) -> bool:
    """D-06: released by reception. Admin too, as they can do anything."""
    return is_privileged(actor)


def is_attendee(actor: User, booking: Booking) -> bool:
    """On the attendee list, which is the only thing that lets them respond."""
    return any(attendee.user_id == actor.id for attendee in booking.attendees)

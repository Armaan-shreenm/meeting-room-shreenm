"""Enumerations backed by PostgreSQL enum types.

Every vocabulary in the system is a real database enum, so an invalid value is
rejected by PostgreSQL and not only by the application.
"""

from __future__ import annotations

import enum


class BookingStatus(str, enum.Enum):
    """Spec section 11.

    A booking is never deleted. CANCELLED and NO_SHOW both release the room
    immediately, because the ``no_double_booking`` exclusion constraint is scoped
    to CONFIRMED rows only.
    """

    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


class AttendeeResponse(str, enum.Enum):
    """An invited colleague's reply.

    Section 9: an attendee cannot cancel a meeting, only decline their own place.
    """

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"


class NotificationEvent(str, enum.Enum):
    """The three events that trigger a mail - spec section 8.

    CHANGED fires on a details-only edit: title, department, conducted_by,
    attendees or the reception note. Room, date and time are never editable, so
    they can never raise a CHANGED.
    """

    BOOKED = "BOOKED"
    CHANGED = "CHANGED"
    CANCELLED = "CANCELLED"


class NotificationStatus(str, enum.Enum):
    """Delivery state of a single queued message."""

    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"


# Names of the PostgreSQL enum types. The initial migration creates these by
# hand, so the names have to agree between the models and the migration.
BOOKING_STATUS_ENUM = "booking_status"
ATTENDEE_RESPONSE_ENUM = "attendee_response"
NOTIFICATION_EVENT_ENUM = "notification_event"
NOTIFICATION_STATUS_ENUM = "notification_status"

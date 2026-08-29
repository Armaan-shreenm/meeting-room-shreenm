"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata`` and installs
the ``before_flush`` guard that keeps ``bookings.booking_date`` honest.
"""

from app.models.booking import Booking, BookingAttendee, BookingDateMismatchError
from app.models.department import Department
from app.models.enums import (
    ATTENDEE_RESPONSE_ENUM,
    BOOKING_STATUS_ENUM,
    NOTIFICATION_EVENT_ENUM,
    NOTIFICATION_STATUS_ENUM,
    USER_ROLE_ENUM,
    AttendeeResponse,
    BookingStatus,
    NotificationEvent,
    NotificationStatus,
    UserRole,
)
from app.models.holiday import Holiday
from app.models.logs import AuditLog, NotificationLog
from app.models.room import Room
from app.models.user import User

__all__ = [
    "ATTENDEE_RESPONSE_ENUM",
    "BOOKING_STATUS_ENUM",
    "NOTIFICATION_EVENT_ENUM",
    "NOTIFICATION_STATUS_ENUM",
    "USER_ROLE_ENUM",
    "AttendeeResponse",
    "AuditLog",
    "Booking",
    "BookingAttendee",
    "BookingDateMismatchError",
    "BookingStatus",
    "Department",
    "Holiday",
    "NotificationEvent",
    "NotificationLog",
    "NotificationStatus",
    "Room",
    "User",
    "UserRole",
]

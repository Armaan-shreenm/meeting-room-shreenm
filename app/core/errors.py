"""Domain errors.

Services raise these; the HTTP layer turns them into responses. Keeping them
free of FastAPI means the booking rules can be tested without a request, and
means a service cannot accidentally invent a status code.

Every one of them carries a message from :mod:`app.core.messages`. There is no
constructor that takes free text, because "Booking failed" is a bug.
"""

from __future__ import annotations


class NmMeetError(Exception):
    """Base class. ``detail`` is always a user-facing string."""

    status_code: int = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ValidationError(NmMeetError):
    """A booking that breaks one of the section 10 rules."""

    status_code = 400


class NotFoundError(NmMeetError):
    """A room, booking, user or department that does not exist."""

    status_code = 404


class PermissionError_(NmMeetError):
    """The actor is not allowed to do this - spec section 9."""

    status_code = 403


class ConflictError(NmMeetError):
    """The room was taken. Layer 2 or layer 3 of spec section 7 refused it.

    ``free_rooms`` is carried so the API can report it alongside the message,
    letting the frontend re-point the user without a second round trip.
    """

    status_code = 409

    def __init__(self, detail: str, free_rooms: list[str] | None = None) -> None:
        super().__init__(detail)
        self.free_rooms = free_rooms or []

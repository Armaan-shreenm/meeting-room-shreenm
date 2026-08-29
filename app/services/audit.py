"""Audit trail - spec section 11.

Section 9 point 6: a booking is marked CANCELLED and kept for the audit record,
never deleted. A no-show release sends no notification at all, so the row this
module writes is the only trace it leaves.

Snapshots are plain JSON-serialisable dicts so a change can be read back without
replaying the application or joining half the schema.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core import time as timeutil
from app.models import AuditLog, Booking, User

logger = logging.getLogger(__name__)

# Actions recorded against a booking.
CREATED = "CREATED"
CANCELLED = "CANCELLED"
UPDATED = "UPDATED"
NO_SHOW = "NO_SHOW"
RESTORED = "RESTORED"
RESPONDED = "RESPONDED"

BOOKING_ENTITY = "booking"


def snapshot(booking: Booking) -> dict[str, Any]:
    """A booking as JSONB, in local terms so it reads without conversion."""
    return {
        "id": str(booking.id),
        "room_id": booking.room_id,
        "booking_date": booking.booking_date.isoformat(),
        "entry": timeutil.to_hhmm(timeutil.minutes_from_midnight(booking.entry_time)),
        "exit": timeutil.to_hhmm(timeutil.minutes_from_midnight(booking.exit_time)),
        "entry_time_utc": booking.entry_time.isoformat(),
        "exit_time_utc": booking.exit_time.isoformat(),
        "department_id": booking.department_id,
        "conducted_by": booking.conducted_by,
        "booked_by": booking.booked_by,
        "title": booking.title,
        "reception_note": booking.reception_note,
        "status": booking.status.value,
        "attendee_ids": sorted(a.user_id for a in booking.attendees),
    }


def record(
    db: Session,
    *,
    action: str,
    booking: Booking,
    actor: User | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Write one audit row. The caller owns the transaction."""
    entry = AuditLog(
        entity=BOOKING_ENTITY,
        entity_id=str(booking.id),
        action=action,
        actor_id=actor.id if actor is not None else None,
        before=before,
        after=after,
    )
    db.add(entry)

    logger.info(
        "audit %s booking=%s actor=%s",
        action,
        booking.id,
        actor.email if actor is not None else "system",
    )
    return entry

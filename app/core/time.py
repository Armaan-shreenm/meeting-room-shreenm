"""Timezone helpers.

Everything is stored as an aware UTC timestamp and displayed in the branch
timezone from settings. These functions are the only place that conversion
happens, so no other module needs to know what Asia/Kolkata is.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.config import settings


def now_utc() -> datetime:
    """Current instant, timezone-aware, in UTC."""
    return datetime.now(tz=timezone.utc)


def now_local() -> datetime:
    """Current instant, timezone-aware, in the branch timezone."""
    return datetime.now(tz=settings.timezone)


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC.

    A naive datetime is read as branch-local time, which is what a user typing
    "14:30" into the booking form means.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=settings.timezone)
    return value.astimezone(timezone.utc)


def to_local(value: datetime) -> datetime:
    """Convert an aware datetime to the branch timezone for display.

    A naive datetime is read as UTC, which is what psycopg2 hands back for a
    timestamptz column when the connection has no timezone set.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(settings.timezone)


def booking_date_for(entry_time: datetime) -> date:
    """Derive ``bookings.booking_date`` from ``bookings.entry_time``.

    This is the single source of truth for that column. ``booking_date`` is the
    branch-local calendar day the meeting starts on, which is the day the grid
    groups by — and it is not the UTC day. A booking that starts at 19:00 UTC is
    already the next morning in Mumbai, so the two dates differ by one for every
    booking whose local start is 00:00 to 05:29.

    A CHECK constraint cannot enforce the relationship, because ``AT TIME ZONE``
    is STABLE rather than IMMUTABLE and PostgreSQL will not accept it in one. The
    guarantee is therefore made in Python: this function is the only way the
    column is ever set, and ``app.models.booking`` refuses to flush a Booking
    whose stored date disagrees with it.
    """
    return to_local(entry_time).date()


def local_date(value: datetime) -> date:
    """The calendar date an instant falls on in the branch timezone."""
    return to_local(value).date()

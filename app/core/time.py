"""Time conversion and formatting - the boundary between the API and storage.

Three representations exist and this module is the only place they meet:

* **Storage** - aware ``datetime`` in UTC, in ``timestamptz`` columns.
* **The API** - an ISO date plus ``"HH:MM"`` local strings. The frontend never
  sees a UTC timestamp and never does timezone arithmetic.
* **Messages** - "1 pm", "1:30 pm", the way the specification writes times.
  :func:`format_clock` is the one formatter; nothing else may format a time.

Internally the day is handled as minutes from local midnight, which is what the
approved prototype works in (09:00 = 540).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from app.config import settings

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

MINUTES_PER_DAY = 24 * 60


# --------------------------------------------------------------------- now


def now_utc() -> datetime:
    """Current instant, timezone-aware, in UTC."""
    return datetime.now(tz=timezone.utc)


def now_local() -> datetime:
    """Current instant, timezone-aware, in the branch timezone."""
    return datetime.now(tz=settings.timezone)


def local_today() -> date:
    """Today's date in Mumbai, which is not always today's date in UTC.

    Every "is this in the past" decision uses this. At 19:00 UTC it is already
    tomorrow at the branch, and a booking for that day must not be refused as
    past.
    """
    return now_local().date()


def now_local_minutes() -> int:
    """Minutes from local midnight, right now."""
    current = now_local()
    return current.hour * 60 + current.minute


# -------------------------------------------------------------- conversion


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
    groups by - and it is not the UTC day. A booking that starts at 19:00 UTC is
    already the next morning in Mumbai.

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


def local_datetime(day: date, minutes: int) -> datetime:
    """Build the UTC instant for a local date and minutes-from-midnight.

    This is the API boundary: ``("2026-08-31", 540)`` becomes the timestamptz
    stored for 09:00 in Mumbai on that date.
    """
    naive = datetime.combine(day, time()) + timedelta(minutes=minutes)
    return to_utc(naive)


def minutes_from_midnight(value: datetime) -> int:
    """Local minutes from midnight for a stored instant."""
    local = to_local(value)
    return local.hour * 60 + local.minute


# ------------------------------------------------------------- parse / print


def parse_hhmm(value: str) -> int:
    """Parse ``"13:30"`` into 810 minutes. Raises ValueError on anything else."""
    match = _HHMM_RE.match(value.strip())
    if match is None:
        raise ValueError(f"{value!r} is not an HH:MM time")
    return int(match.group(1)) * 60 + int(match.group(2))


def to_hhmm(minutes: int) -> str:
    """Render minutes from midnight as ``"13:30"`` for the API."""
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise ValueError(f"{minutes} is outside a single day")
    # 24:00 is a legal end-of-day value; keep it rather than wrapping to 00:00.
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_clock(minutes: int) -> str:
    """Render minutes from midnight the way the specification writes times.

    540 -> "9 am", 780 -> "1 pm", 810 -> "1:30 pm", 1200 -> "8 pm".

    This is the only time formatter for user-facing text. Messages never contain
    "13:00".
    """
    hour, minute = divmod(minutes, 60)
    meridiem = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    if minute:
        return f"{display_hour}:{minute:02d} {meridiem}"
    return f"{display_hour} {meridiem}"


def format_clock_range(entry: int, exit_: int) -> str:
    """"1 pm to 3 pm", for messages that name a window."""
    return f"{format_clock(entry)} to {format_clock(exit_)}"

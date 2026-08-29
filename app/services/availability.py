"""Availability — spec sections 1, 4 and 5.

The rule that matters most: **availability is always per room, never global.**
If Power is booked 1 pm to 3 pm, that window is unavailable for Power and fully
available for Spark, Pulse, Ignite and Switch.

Two bookings clash when all three are true: same room, same date, and the times
overlap, where overlap means ``new.entry < existing.exit AND new.exit >
existing.entry``. The strict comparisons are what make back-to-back legal — a
meeting ending at 15:00 does not clash with one starting at 15:00.

Everything here works in minutes from local midnight, which is what the approved
prototype works in. Conversion to stored UTC happens only at the edges, in
:mod:`app.core.time`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core import time as timeutil
from app.models import Booking, BookingStatus, Holiday, Room


# --------------------------------------------------------------------- types


@dataclass(frozen=True)
class Window:
    """A half-open [entry, exit) window in local minutes from midnight."""

    entry: int
    exit: int

    def overlaps(self, other: "Window") -> bool:
        """Spec section 5. Note the strict < and >: back to back is allowed."""
        return self.entry < other.exit and self.exit > other.entry


@dataclass
class RoomDay:
    """One room's picture of one day."""

    room: Room
    bookings: list[Booking] = field(default_factory=list)

    @property
    def windows(self) -> list[Window]:
        return [
            Window(
                timeutil.minutes_from_midnight(b.entry_time),
                timeutil.minutes_from_midnight(b.exit_time),
            )
            for b in self.bookings
        ]


@dataclass
class DayStatus:
    """Whether the branch is open on a date, and if not, why."""

    closed: bool
    reason: str | None = None
    holiday_name: str | None = None


# ------------------------------------------------------------------- slots


def slot_starts() -> list[int]:
    """Every bookable start time, in local minutes. 09:00..19:30 by default."""
    return list(
        range(settings.open_minutes, settings.close_minutes, settings.slot_minutes)
    )


def is_on_slot_boundary(minutes: int) -> bool:
    """Spec section 4: only :00 and :30 may be selected."""
    return minutes % settings.slot_minutes == 0


# ------------------------------------------------------------- day status


def get_holiday(db: Session, day: date) -> Holiday | None:
    return db.scalar(select(Holiday).where(Holiday.date == day))


def day_status(db: Session, day: date) -> DayStatus:
    """Is the office open on this date?

    Past dates, Sundays (D-03) and declared holidays are all closed. The past
    check uses the branch's today, not the server's UTC today.
    """
    if day < timeutil.local_today():
        return DayStatus(closed=True, reason="past")

    if day.weekday() in settings.closed_weekdays:
        return DayStatus(closed=True, reason="weekly_closure")

    holiday = get_holiday(db, day)
    if holiday is not None:
        return DayStatus(closed=True, reason="holiday", holiday_name=holiday.name)

    return DayStatus(closed=False)


def is_too_far_ahead(day: date) -> bool:
    """Spec field 2: today to today + 90 days."""
    return (day - timeutil.local_today()).days > settings.max_advance_days


# ---------------------------------------------------------------- queries


def confirmed_bookings(
    db: Session, day: date, room_id: str | None = None
) -> list[Booking]:
    """Every CONFIRMED booking on a local date, oldest start first.

    CANCELLED and NO_SHOW rows are excluded, which is what frees their window
    for everyone the instant the status changes.
    """
    statement = (
        select(Booking)
        .where(
            Booking.booking_date == day,
            Booking.status == BookingStatus.CONFIRMED,
        )
        .options(
            selectinload(Booking.conductor),
            selectinload(Booking.booker),
            selectinload(Booking.department),
        )
        .order_by(Booking.entry_time)
    )
    if room_id is not None:
        statement = statement.where(Booking.room_id == room_id)
    return list(db.scalars(statement).all())


def rooms_in_order(db: Session) -> list[Room]:
    return list(db.scalars(select(Room).order_by(Room.display_order)).all())


def day_by_room(db: Session, day: date) -> list[RoomDay]:
    """The whole grid for one date: every room with its confirmed bookings."""
    rooms = rooms_in_order(db)
    bookings = confirmed_bookings(db, day)

    grouped: dict[str, list[Booking]] = {room.id: [] for room in rooms}
    for booking in bookings:
        # A booking for a room that no longer exists cannot happen (the FK is
        # RESTRICT), but do not let a stray row break the whole grid.
        grouped.setdefault(booking.room_id, []).append(booking)

    return [RoomDay(room=room, bookings=grouped.get(room.id, [])) for room in rooms]


# ----------------------------------------------------------- free / blocked


def is_blocked_at(windows: list[Window], minutes: int) -> bool:
    """Is this slot start inside an existing booking for the room?"""
    return any(w.entry <= minutes < w.exit for w in windows)


def is_window_free(windows: list[Window], candidate: Window) -> bool:
    """Does the candidate avoid every existing booking for the room?"""
    return not any(candidate.overlaps(w) for w in windows)


def free_slot_count(windows: list[Window], day: date) -> int:
    """How many slot starts are still bookable — the prototype's freeCountFor.

    Slots inside an existing booking do not count. On a past date nothing counts,
    and today the slots that have already gone do not count either.
    """
    today = timeutil.local_today()
    if day < today:
        return 0

    floor = timeutil.now_local_minutes() if day == today else 0

    return sum(
        1
        for start in slot_starts()
        if not is_blocked_at(windows, start) and start >= floor
    )


def exit_cap(windows: list[Window], entry: int) -> int:
    """Latest permitted exit for an entry — the prototype's capAfter().

    ``min(next booking start, entry + max length, closing time)``. Section 4's
    worked example: Power free from 12:00 but booked from 13:00, an entry of
    12:30 allows an exit of 13:00 only.
    """
    next_entries = [w.entry for w in windows if w.entry >= entry]
    next_start = min(next_entries) if next_entries else settings.close_minutes
    return min(
        next_start,
        entry + settings.max_booking_minutes,
        settings.close_minutes,
    )


def next_booking_start(windows: list[Window], entry: int) -> int | None:
    """Start of the first booking at or after ``entry``, if there is one."""
    later = [w.entry for w in windows if w.entry >= entry]
    return min(later) if later else None


# ------------------------------------------------- clash message ingredients


def free_rooms_for_window(
    db: Session, day: date, window: Window, exclude_room_id: str | None = None
) -> list[Room]:
    """Which rooms are free for this exact window, in display order.

    Used to build the section 5 message. Never hardcoded — a room is listed only
    if the database says it is actually free.
    """
    free: list[Room] = []
    for room_day in day_by_room(db, day):
        if exclude_room_id is not None and room_day.room.id == exclude_room_id:
            continue
        if is_window_free(room_day.windows, window):
            free.append(room_day.room)
    return free


def first_free_room(
    db: Session, day: date, duration: int, not_before: int
) -> tuple[Room, int] | None:
    """Earliest room and time that can hold a meeting of ``duration`` minutes.

    Walks the day's slots from ``not_before`` onward and returns the first
    (room, start) that fits, earliest time first and display order as the
    tie-break. This is what fills in "The first free room is Pulse at 3 pm".
    """
    day_rooms = day_by_room(db, day)
    latest_start = settings.close_minutes - duration

    best: tuple[Room, int] | None = None

    for room_day in day_rooms:
        windows = room_day.windows
        for start in slot_starts():
            if start < not_before or start > latest_start:
                continue
            if is_window_free(windows, Window(start, start + duration)):
                if best is None or start < best[1]:
                    best = (room_day.room, start)
                break  # earliest fit for this room; no later one can beat it

    return best

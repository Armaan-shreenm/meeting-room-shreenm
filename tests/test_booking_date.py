"""booking_date must always agree with entry_time.

The two columns are stored independently and can drift. A CHECK constraint
cannot enforce the relationship because ``AT TIME ZONE`` is STABLE, not
IMMUTABLE, and PostgreSQL refuses it in a CHECK. The guarantee is therefore made
in Python, and this module is what proves it holds.

These tests need the live development database from the README. Run with:

    pytest -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.core.time import booking_date_for, to_local, to_utc
from app.database import SessionLocal
from app.models import (
    Booking,
    BookingDateMismatchError,
    BookingStatus,
    Department,
    Room,
    User,
)

TEST_TITLE_PREFIX = "pytest booking_date"

# Far enough ahead to never collide with a real booking.
BASE_DATE = date(2098, 3, 10)


@pytest.fixture()
def db():
    """A session that removes anything the test wrote."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            delete(Booking).where(Booking.title.like(f"{TEST_TITLE_PREFIX}%"))
        )
        session.commit()
        session.close()


@pytest.fixture()
def refs(db):
    """Seeded room, department and user to hang a booking off."""
    room = db.get(Room, "spark")
    department = db.scalar(select(Department).order_by(Department.id))
    user = db.scalar(select(User).order_by(User.id))
    if not all((room, department, user)):
        pytest.skip("Reference data missing — run `python -m scripts.seed` first.")
    return room, department, user


def build(refs, entry: datetime, *, booking_date=None, hours: int = 1) -> Booking:
    room, department, user = refs
    return Booking(
        id=uuid.uuid4(),
        room_id=room.id,
        booking_date=booking_date,
        entry_time=entry,
        exit_time=entry + timedelta(hours=hours),
        department_id=department.id,
        conducted_by=user.id,
        booked_by=user.id,
        title=f"{TEST_TITLE_PREFIX} {entry.isoformat()}",
        status=BookingStatus.CONFIRMED,
    )


# --------------------------------------------------------------- the helper


def test_booking_date_for_uses_branch_timezone():
    """13:00 IST is the same calendar day in Mumbai."""
    entry = to_utc(datetime.combine(BASE_DATE, datetime.min.time()).replace(hour=13))
    assert booking_date_for(entry) == BASE_DATE


def test_booking_date_for_1900_utc_is_next_day_in_mumbai():
    """19:00 UTC is 00:30 the following morning in Asia/Kolkata.

    This is the case the whole guard exists for: taking the UTC date would file
    the booking under the wrong day and it would vanish from the grid.
    """
    entry = datetime(2098, 3, 10, 19, 0, tzinfo=timezone.utc)

    assert to_local(entry).strftime("%Y-%m-%d %H:%M") == "2098-03-11 00:30"
    assert entry.date() == date(2098, 3, 10)          # the UTC day
    assert booking_date_for(entry) == date(2098, 3, 11)  # the Mumbai day


def test_booking_date_for_1829_utc_is_still_the_same_day():
    """18:29 UTC is 23:59 IST — the last minute that stays on the same date."""
    entry = datetime(2098, 3, 10, 18, 29, tzinfo=timezone.utc)
    assert booking_date_for(entry) == date(2098, 3, 10)


def test_booking_date_for_reads_naive_as_branch_local():
    """A naive datetime is what a user typing 14:30 into the form means."""
    assert booking_date_for(datetime(2098, 3, 10, 14, 30)) == BASE_DATE


# ------------------------------------------------------------- the flush guard


def test_missing_booking_date_is_derived_on_flush(db, refs):
    """The column cannot be forgotten."""
    entry = to_utc(datetime(2098, 3, 10, 11, 0))
    booking = build(refs, entry, booking_date=None)

    db.add(booking)
    db.flush()

    assert booking.booking_date == date(2098, 3, 10)


def test_disagreeing_booking_date_is_refused(db, refs):
    """A wrong date is an error, not something to quietly correct."""
    entry = to_utc(datetime(2098, 3, 10, 11, 0))
    booking = build(refs, entry, booking_date=date(2098, 3, 11))

    db.add(booking)
    with pytest.raises(BookingDateMismatchError) as excinfo:
        db.flush()

    assert "2098-03-11" in str(excinfo.value)
    assert "Asia/Kolkata" in str(excinfo.value)


def test_1900_utc_booking_stores_the_mumbai_date(db, refs):
    """End to end: the late-evening UTC case reaches the database correctly."""
    entry = datetime(2098, 3, 10, 19, 0, tzinfo=timezone.utc)
    booking = build(refs, entry, booking_date=None)

    db.add(booking)
    db.flush()
    db.commit()

    stored = db.scalar(select(Booking).where(Booking.id == booking.id))
    assert stored.booking_date == date(2098, 3, 11)
    assert to_local(stored.entry_time).hour == 0
    assert to_local(stored.entry_time).minute == 30


def test_utc_date_would_have_been_wrong(db, refs):
    """Guards against someone 'simplifying' the helper to entry_time.date()."""
    entry = datetime(2098, 3, 10, 19, 0, tzinfo=timezone.utc)
    booking = build(refs, entry, booking_date=None)

    db.add(booking)
    db.flush()

    assert booking.booking_date != entry.date()


def test_changing_entry_time_without_the_date_is_refused(db, refs):
    """An update that moves the booking across the local date boundary is caught."""
    entry = to_utc(datetime(2098, 3, 10, 11, 0))
    booking = build(refs, entry, booking_date=None)
    db.add(booking)
    db.flush()
    db.commit()

    # Push it to 00:30 the next morning in Mumbai, leaving booking_date behind.
    booking.entry_time = datetime(2098, 3, 10, 19, 0, tzinfo=timezone.utc)
    booking.exit_time = datetime(2098, 3, 10, 20, 0, tzinfo=timezone.utc)

    with pytest.raises(BookingDateMismatchError):
        db.flush()

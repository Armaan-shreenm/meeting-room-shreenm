"""Shared fixtures. These tests run against the live development database.

Start it as the README describes before running pytest:

    pg_ctl -D .pgdata -l .pgdata\\logfile -o "-p 5432 -h 127.0.0.1" start
    alembic upgrade head
    python -m scripts.seed
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.core.middleware import reset_rate_limits
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    issue_session,
)
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Booking, Department, Holiday, Room, User

# Every booking a test creates carries this, so cleanup can find them all.
TEST_TITLE_PREFIX = "pytest"


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _purge(session) -> None:
    """Remove test bookings and everything hanging off them.

    booking_attendees and notification_log cascade with the booking. audit_log
    does not — it references the entity by id as text on purpose, so that the
    record outlives what it describes — so those rows are removed by hand.
    """
    session.rollback()

    doomed = session.scalars(
        select(Booking.id).where(Booking.title.like(f"{TEST_TITLE_PREFIX}%"))
    ).all()
    if doomed:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_id.in_([str(i) for i in doomed])
            )
        )

    session.execute(delete(Booking).where(Booking.title.like(f"{TEST_TITLE_PREFIX}%")))
    session.execute(delete(Holiday).where(Holiday.name.like(f"{TEST_TITLE_PREFIX}%")))
    session.commit()


@pytest.fixture(autouse=True)
def clean_slate():
    """Remove test rows before and after every test, so order never matters.

    The rate limiter's counters are cleared too. They are process-global and
    would otherwise leak between cases: a suite that books thirty rooms in two
    seconds is nothing like a person doing it, and the limiter would rightly
    refuse the later tests. test_hardening drives the limiter to 429 inside a
    single test, which is where that rule belongs.
    """
    reset_rate_limits()
    session = SessionLocal()
    try:
        _purge(session)
        yield
        _purge(session)
    finally:
        session.close()
        reset_rate_limits()


@pytest.fixture()
def client():
    """The real app. /api routes are registered before the StaticFiles mount."""
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------- people


@pytest.fixture()
def users(db) -> dict[str, User]:
    """Seeded directory members, by a short handle."""
    by_email = {u.email: u for u in db.scalars(select(User)).all()}
    wanted = {
        "priya": "priya.nair@shreenm.com",
        "rahul": "rahul.mehta@shreenm.com",
        "sana": "sana.qureshi@shreenm.com",
        "aditi": "aditi.shah@shreenm.com",
        "vikram": "vikram.rao@shreenm.com",
        "imran": "imran.sheikh@shreenm.com",
        "neha": "neha.kulkarni@shreenm.com",
        "joseph": "joseph.dsouza@shreenm.com",
        "reception": "reception.mumbai@shreenm.com",
        "admin": "admin@shreenm.com",
    }
    missing = [e for e in wanted.values() if e not in by_email]
    if missing:
        pytest.skip(f"Directory not seeded, missing {missing}. Run scripts.seed.")
    return {handle: by_email[email] for handle, email in wanted.items()}


@pytest.fixture()
def departments(db) -> dict[str, Department]:
    rows = {d.name: d for d in db.scalars(select(Department)).all()}
    if not rows:
        pytest.skip("Departments not seeded. Run scripts.seed.")
    return rows


@pytest.fixture()
def rooms(db) -> dict[str, Room]:
    rows = {r.id: r for r in db.scalars(select(Room)).all()}
    if len(rows) < 5:
        pytest.skip("Rooms not seeded. Run scripts.seed.")
    return rows


# Any value works for the double-submit check, as long as cookie and header
# agree - the server compares them to each other, not to anything stored.
TEST_CSRF = "pytest-csrf-token"


def headers_for(user: User) -> dict[str, str]:
    """Act as this directory member, with a real signed session.

    Phase 5 replaced the X-User-Email header with a session cookie. Sending the
    cookies explicitly keeps every existing test calling headers_for() unchanged.
    """
    token = issue_session(user.id)
    return {
        "Cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={TEST_CSRF}",
        CSRF_HEADER: TEST_CSRF,
    }


# -------------------------------------------------------------------- dates


def is_working_day(day: date) -> bool:
    """Mon-Sat are working days; Sunday is closed (D-03)."""
    return day.weekday() not in settings.closed_weekdays


def working_day(offset: int = 30) -> date:
    """A working day at least ``offset`` days out and inside the 90-day window."""
    candidate = date.today() + timedelta(days=offset)
    while not is_working_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def next_sunday(offset: int = 1) -> date:
    """The next Sunday at least ``offset`` days out."""
    candidate = date.today() + timedelta(days=offset)
    while candidate.weekday() != 6:
        candidate += timedelta(days=1)
    return candidate


def beyond_horizon() -> date:
    """A working day past the 90-day limit, so the horizon rule is what fires."""
    candidate = date.today() + timedelta(days=settings.max_advance_days + 1)
    while not is_working_day(candidate):
        candidate += timedelta(days=1)
    return candidate


@pytest.fixture()
def day() -> date:
    """The default test date: a working day 30 days out."""
    return working_day(30)


# ------------------------------------------------------------------ helpers


def booking_payload(
    room_id: str,
    day: date,
    entry: str,
    exit_: str,
    department_id: int,
    conducted_by: int,
    *,
    attendee_ids: list[int] | None = None,
    title: str | None = None,
    reception_note: str | None = None,
) -> dict:
    return {
        "room_id": room_id,
        "date": day.isoformat(),
        "entry": entry,
        "exit": exit_,
        "department_id": department_id,
        "conducted_by": conducted_by,
        "attendee_ids": attendee_ids or [],
        "title": title or f"{TEST_TITLE_PREFIX} {room_id} {entry}",
        "reception_note": reception_note,
    }

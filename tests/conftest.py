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
from app.database import SessionLocal, engine
from app.main import app
from app.services import notifications
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
    """Empty the bookings table between tests.

    It used to remove only rows titled "pytest...". That stopped working when
    the booking form dropped its title field: every booking is now called
    "Meeting", so a real one made in the browser looked like a test row to
    nobody and silently collided with the fixtures. Clearing the table is the
    honest version - and the guard in ``_refuse_to_run_against_production``
    is what makes it safe.

    booking_attendees and notification_log cascade with the booking. audit_log
    does not - it references the entity by id as text on purpose, so that the
    record outlives what it describes - so those rows go first.
    """
    session.rollback()
    session.execute(delete(AuditLog))
    session.execute(delete(Booking))
    session.execute(delete(Holiday).where(Holiday.name.like(f"{TEST_TITLE_PREFIX}%")))
    session.commit()


def _refuse_to_run_against_production() -> None:
    """The suite empties the bookings table. Never let that touch production."""
    if settings.is_production:
        raise RuntimeError(
            "Refusing to run the test suite with ENVIRONMENT=production: it "
            "deletes every booking. Point DATABASE_URL at a development "
            "database first."
        )
    host = str(engine.url.host or "")
    if host not in ("", "localhost", "127.0.0.1", "::1"):
        raise RuntimeError(
            f"Refusing to run the test suite against {host}: it deletes every "
            "booking, and that is not a local database."
        )


_refuse_to_run_against_production()


@pytest.fixture(autouse=True)
def signed_in_mode(monkeypatch):
    """Run the suite with sign-in switched on.

    The shipped default is off: nobody signs in and the host named in the form
    owns the booking. The session, CSRF and ownership machinery is still built
    and still has to work for the day Google Sign-In is turned on, so the bulk
    of the suite exercises it. tests/test_open_access.py covers the shipped
    default instead.
    """
    monkeypatch.setattr(settings, "sign_in_required", True)


@pytest.fixture()
def open_access(monkeypatch):
    """The shipped default: no sign-in at all."""
    monkeypatch.setattr(settings, "sign_in_required", False)


@pytest.fixture(autouse=True)
def notifications_stay_off_the_network(monkeypatch):
    """No test ever sends real mail.

    Three tests open the app's lifespan (``with TestClient(app)``), which calls
    configure_transport() and installs whatever .env describes. The transport is
    a module-level global, so it then stays installed for every test that runs
    afterwards - and with real SMTP credentials in .env that means the suite
    starts trying to reach a mail server, slowly and from inside assertions
    about something else entirely.

    Both ends are pinned: the switch is off, so a lifespan that does run picks
    stdout, and the transport is put back to stdout before each test in case one
    already did.
    """
    monkeypatch.setattr(settings, "notifications_enabled", False)
    # Synchronous too: an assertion about a SENT row cannot wait on a thread,
    # and the stdout transport takes no time worth deferring. The async path has
    # its own tests, which turn it back on deliberately.
    monkeypatch.setattr(settings, "notifications_async", False)
    notifications.set_transport(notifications.StdoutTransport())


@pytest.fixture(autouse=True)
def no_announcement_address(monkeypatch):
    """No extra announcement recipient unless a test asks for one.

    BOOKING_ANNOUNCE_EMAIL is a deployment's choice and it lives in .env, so
    leaving it ambient would make the section 8 recipient counts pass or fail
    depending on whose laptop the suite is running on. Tests that care about it
    set it themselves.
    """
    monkeypatch.setattr(settings, "booking_announce_email", "")


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


def bookable_days() -> list[date]:
    """Every day a booking may legally land on, today first.

    The window is only a week now, so tests share it. The clean_slate fixture
    purges between cases, which is what makes reuse safe.
    """
    today = date.today()
    days = [
        today + timedelta(days=n)
        for n in range(settings.max_advance_days)
    ]
    return [d for d in days if is_working_day(d)]


def working_day(offset: int = 1) -> date:
    """A working day inside the booking window.

    ``offset`` is an index into the open days, not a number of days. It wraps,
    because a seven-day window cannot give every test its own date and does not
    need to.
    """
    days = bookable_days()
    # Skip today where possible: its earlier slots have already passed.
    future = [d for d in days if d > date.today()] or days
    return future[(max(offset, 1) - 1) % len(future)]


def next_sunday(offset: int = 1) -> date:
    """The next Sunday at least ``offset`` days out."""
    candidate = date.today() + timedelta(days=offset)
    while candidate.weekday() != 6:
        candidate += timedelta(days=1)
    return candidate


def beyond_horizon() -> date:
    """A working day just past the window, so the horizon rule is what fires."""
    candidate = date.today() + timedelta(days=settings.max_advance_days)
    while not is_working_day(candidate):
        candidate += timedelta(days=1)
    return candidate


@pytest.fixture()
def day() -> date:
    """The default test date: the next working day inside the booking window."""
    return working_day(1)


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

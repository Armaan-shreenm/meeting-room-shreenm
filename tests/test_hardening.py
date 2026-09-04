"""Phase 7 - rate limiting, request ids, error handling and the SMTP transport."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.core.middleware import REQUEST_ID_HEADER
from app.main import app
from app.models import (
    Booking,
    BookingStatus,
    NotificationEvent,
)
from app.services import notifications
from app.services.transports import SmtpTransport, configure_transport
from tests.conftest import (
    TEST_TITLE_PREFIX,
    booking_payload,
    headers_for,
)


def post_booking(client, user, **kwargs):
    return client.post(
        "/api/bookings", json=booking_payload(**kwargs), headers=headers_for(user)
    )


# =============================================================================
# Request ids
# =============================================================================


def test_every_response_carries_a_request_id(client, users):
    response = client.get("/api/rooms", headers=headers_for(users["priya"]))
    assert response.status_code == 200
    assert response.headers.get(REQUEST_ID_HEADER)
    assert len(response.headers[REQUEST_ID_HEADER]) >= 8


def test_an_upstream_request_id_is_honoured(client, users):
    """So a request can be traced across a proxy."""
    response = client.get(
        "/api/rooms",
        headers={**headers_for(users["priya"]), REQUEST_ID_HEADER: "trace-me-123"},
    )
    assert response.headers[REQUEST_ID_HEADER] == "trace-me-123"


def test_two_requests_get_different_ids(client, users):
    first = client.get("/api/rooms", headers=headers_for(users["priya"]))
    second = client.get("/api/rooms", headers=headers_for(users["priya"]))
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


# =============================================================================
# The global error handler
# =============================================================================


def test_an_unexpected_error_never_leaks_a_stack_trace(users):
    """A crash must produce a sentence and a reference, not a traceback."""
    boom = APIRouter()

    @boom.get("/_boom")
    def explode():
        raise RuntimeError("secret internal detail: table users, column password_hash")

    # Inserted at the front: the StaticFiles mount at "/" is a catch-all and
    # would otherwise swallow this path.
    app.include_router(boom, prefix="/api")
    app.router.routes.insert(0, app.router.routes.pop())
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/_boom", headers=headers_for(users["priya"]))

        assert response.status_code == 500
        detail = response.json()["detail"]

        # The user is told what to do and given something to quote.
        assert detail.startswith("Something went wrong at our end")
        request_id = response.headers[REQUEST_ID_HEADER]
        assert request_id in detail

        # And nothing about the internals escapes.
        for leak in ("Traceback", "RuntimeError", "password_hash", "table users"):
            assert leak not in response.text, leak
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/_boom"
        ]


# =============================================================================
# Rate limiting
# =============================================================================


def test_booking_creation_is_rate_limited(client, users, departments, rooms, day):
    """The limit protects writes; the message says what to do next."""
    limit = settings.rate_limit_bookings
    statuses = []

    # Deliberately invalid windows: they are refused before touching the
    # database, so this measures the limiter and not booking capacity.
    for _ in range(limit + 3):
        response = client.post(
            "/api/bookings",
            json=booking_payload(
                room_id="spark",
                day=day,
                entry="15:00",
                exit_="14:00",           # exit before entry - always a 400
                department_id=departments["IT"].id,
                conducted_by=users["priya"].id,
            ),
            headers=headers_for(users["priya"]),
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert response.json()["detail"] == messages.TOO_MANY_REQUESTS
            assert response.headers["Retry-After"] == str(
                settings.rate_limit_window_seconds
            )
            break

    assert 429 in statuses, f"never hit the limit in {len(statuses)} attempts"


def test_reads_are_never_rate_limited(client, users, day):
    """The grid polls availability every minute; throttling it helps nobody."""
    for _ in range(settings.rate_limit_bookings + 10):
        response = client.get(
            f"/api/availability?date={day.isoformat()}",
            headers=headers_for(users["priya"]),
        )
        assert response.status_code == 200


# =============================================================================
# Transports
# =============================================================================


def test_stdout_is_the_transport_when_smtp_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "notifications_enabled", False)
    assert configure_transport() == "stdout"
    assert notifications.get_transport().name == "stdout"


def test_smtp_is_selected_when_enabled_and_configured(monkeypatch):
    monkeypatch.setattr(settings, "notifications_enabled", True)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    try:
        assert configure_transport() == "smtp"
        assert notifications.get_transport().name == "smtp"
    finally:
        monkeypatch.setattr(settings, "notifications_enabled", False)
        configure_transport()


def test_a_half_configured_relay_falls_back_rather_than_losing_mail(monkeypatch):
    """Section 8: notifications are always sent. Silence is the worst outcome."""
    monkeypatch.setattr(settings, "notifications_enabled", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    try:
        assert configure_transport() == "stdout"
    finally:
        monkeypatch.setattr(settings, "notifications_enabled", False)
        configure_transport()


def test_smtp_transport_builds_a_well_formed_message(db, users, departments, rooms, day):
    """Built without connecting: no network in the test suite."""
    booking = Booking(
        id=uuid.uuid4(),
        room_id="power",
        booking_date=day,
        entry_time=timeutil.local_datetime(day, 780),
        exit_time=timeutil.local_datetime(day, 900),
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        booked_by=users["rahul"].id,
        title=f"{TEST_TITLE_PREFIX} smtp shape",
        status=BookingStatus.CONFIRMED,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    recipient = notifications.Recipient(
        email="someone@shreenm.com", name="Some One", kind=notifications.ATTENDEE
    )
    rendered = notifications.render(booking, NotificationEvent.BOOKED, recipient)

    transport = SmtpTransport(
        host="smtp.example.invalid",
        port=587,
        from_email="nm-meet@shreenm.com",
        from_name="NM Meet",
    )
    mail = transport.build(rendered)

    assert mail["To"] == "Some One <someone@shreenm.com>"
    assert mail["From"] == "NM Meet <nm-meet@shreenm.com>"
    assert "Power" in mail["Subject"]
    assert "1 pm to 3 pm" in mail.get_content()

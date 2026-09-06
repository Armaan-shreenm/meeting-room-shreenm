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
from app.services import transports
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

    # Both parts, in the order the standard wants: a client that shows no HTML
    # must still get the whole message, and it is the first part it will find.
    text = mail.get_body(preferencelist=("plain",))
    html = mail.get_body(preferencelist=("html",))
    assert text is not None and html is not None
    assert "1 pm to 3 pm" in text.get_content()
    assert "1 pm to 3 pm" in html.get_content()
    assert "Power" in html.get_content()

    # The logo travels with the message, so it renders with the app asleep and
    # without telling the sender who opened the mail.
    cids = [
        part.get("Content-ID")
        for part in mail.walk()
        if part.get_content_type() == "image/png"
    ]
    assert cids == [f"<{notifications.LOGO_CID}>"]
    assert f"cid:{notifications.LOGO_CID}" in html.get_content()


def test_smtp_never_dials_over_ipv6(monkeypatch):
    """The socket must be IPv4, whatever the resolver would otherwise offer.

    Render's containers have no IPv6 route. Left to itself smtplib walks
    everything getaddrinfo returns, hits an AAAA record and dies with
    `[Errno 101] Network is unreachable` - which is what production did, on
    every single notification, while HTTPS to Google worked fine from the same
    process.
    """
    import socket

    asked = {}
    real = socket.getaddrinfo

    def spy(host, port, family=0, *args, **kwargs):
        asked["family"] = family
        raise socket.gaierror("no lookups in the test suite")

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("fell back to a dual-stack connect")
        )
    )

    with pytest.raises(AssertionError):
        transports._ipv4_socket("smtp.example.invalid", 587, 1)

    assert asked["family"] == socket.AF_INET

    # And the transport actually uses it, rather than plain smtplib.
    import inspect

    assert "_IPv4SMTP" in inspect.getsource(transports.SmtpTransport.send)
    assert issubclass(transports._IPv4SMTP, __import__("smtplib").SMTP)


# =============================================================================
# The Gmail API transport
# =============================================================================
# Render's free instances block outbound 25, 465 and 587, so SMTP cannot work
# there at all. HTTPS is open, and this goes out over it.


@pytest.fixture()
def gmail_configured(monkeypatch):
    monkeypatch.setattr(settings, "notifications_enabled", True)
    monkeypatch.setattr(settings, "gmail_client_id", "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "gmail_client_secret", "test-secret")
    monkeypatch.setattr(settings, "gmail_refresh_token", "test-refresh-token")


def test_gmail_wins_over_smtp_when_both_are_configured(gmail_configured, monkeypatch):
    """A host that blocks SMTP is the reason this exists, so it goes first."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    try:
        assert configure_transport() == "gmail-api"
        assert notifications.get_transport().name == "gmail-api"
    finally:
        monkeypatch.setattr(settings, "notifications_enabled", False)
        configure_transport()


def test_smtp_is_still_used_when_there_is_no_refresh_token(monkeypatch):
    monkeypatch.setattr(settings, "notifications_enabled", True)
    monkeypatch.setattr(settings, "gmail_refresh_token", "")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    try:
        assert configure_transport() == "smtp"
    finally:
        monkeypatch.setattr(settings, "notifications_enabled", False)
        configure_transport()


def test_gmail_sends_the_message_base64url_encoded(monkeypatch, users, departments, rooms, day):
    """What Gmail is actually handed: the whole RFC822 message, url-safe base64.

    Plain base64 would be wrong in a way that only shows up on some messages -
    `+` and `/` appear once the body happens to encode to them - so the padding
    and alphabet are worth pinning rather than trusting.
    """
    import base64

    sent = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"access_token": "an-access-token"}

    def fake_post(url, **kwargs):
        if url.endswith("/token"):
            return _Response()
        sent["url"] = url
        sent["auth"] = kwargs["headers"]["Authorization"]
        sent["raw"] = kwargs["json"]["raw"]
        return _Response()

    monkeypatch.setattr(transports.httpx, "post", fake_post)

    transport = transports.GmailApiTransport(
        client_id="id", client_secret="secret", refresh_token="refresh",
        from_email="reception@shreenm.com", from_name="NM Meet - Reception",
    )
    recipient = notifications.Recipient(
        email="priya.nair@shreenm.com", name="Priya Nair", kind=notifications.ATTENDEE
    )
    transport.send(
        notifications.RenderedMessage(
            recipient=recipient,
            event=NotificationEvent.BOOKED,
            subject="Room booked: Power",
            body="Room          Power",
        )
    )

    assert sent["url"].startswith("https://gmail.googleapis.com/")
    assert sent["auth"] == "Bearer an-access-token"

    decoded = base64.urlsafe_b64decode(sent["raw"]).decode("utf-8")
    assert "Subject: Room booked: Power" in decoded
    assert "To: Priya Nair <priya.nair@shreenm.com>" in decoded
    assert "reception@shreenm.com" in decoded
    assert "Room          Power" in decoded


def test_a_refused_refresh_token_is_an_error_not_a_silent_pass(monkeypatch):
    """A revoked token, or one from a consent screen still in Testing after its
    seven days, must fail loudly enough to reach the notification log."""

    class _Refused:
        status_code = 400
        text = '{"error": "invalid_grant"}'

        def json(self):
            return {}

    monkeypatch.setattr(transports.httpx, "post", lambda url, **kw: _Refused())

    transport = transports.GmailApiTransport(
        client_id="id", client_secret="secret", refresh_token="stale",
    )
    with pytest.raises(RuntimeError, match="refresh token"):
        transport.send(
            notifications.RenderedMessage(
                recipient=notifications.Recipient(
                    email="x@shreenm.com", name="X", kind=notifications.ATTENDEE
                ),
                event=NotificationEvent.BOOKED,
                subject="s",
                body="b",
            )
        )

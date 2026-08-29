"""Phase 5 - sessions, CSRF and role enforcement.

Two things are being proved here.

First, that authentication works: a password is checked against a bcrypt hash, a
signed cookie carries the session, and a tampered or expired cookie is refused.

Second, and more important, that **the server enforces section 9 itself**. The
frontend hides what a user may not do, but every one of these tests goes
straight at the API with a session for somebody who should be refused.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select

from app.config import settings
from app.core import messages
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    generate_password,
    hash_password,
    issue_session,
    read_session,
    verify_password,
)
from app.main import app
from app.models import User
from tests.conftest import (
    TEST_CSRF,
    TEST_TITLE_PREFIX,
    booking_payload,
    headers_for,
)

DEV_PASSWORD = "nmmeet-dev"


def post_booking(client, user, **kwargs):
    return client.post(
        "/api/bookings", json=booking_payload(**kwargs), headers=headers_for(user)
    )


# =============================================================================
# Password hashing
# =============================================================================


def test_password_round_trips_through_bcrypt():
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$2b$")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_the_same_password_hashes_differently_each_time():
    """Per-password salt: two identical passwords must not share a hash."""
    assert hash_password("same") != hash_password("same")


def test_a_user_with_no_password_can_never_sign_in():
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False


def test_generated_passwords_avoid_ambiguous_characters():
    for _ in range(20):
        assert not set(generate_password()) & set("0O1lI")


# =============================================================================
# Session tokens
# =============================================================================


def test_session_token_round_trips():
    assert read_session(issue_session(42)) == 42


def test_a_tampered_session_token_is_refused():
    token = issue_session(42)
    assert read_session(token[:-3] + "aaa") is None


def test_a_token_signed_with_another_key_is_refused():
    """The signature is what stops a forged session."""
    forged = URLSafeTimedSerializer("some-other-secret", salt="nm-meet-session")
    assert read_session(forged.dumps({"uid": 42})) is None


def test_an_expired_session_token_is_refused(monkeypatch):
    """itsdangerous stamps whole seconds, so max_age=0 plus a short sleep is the
    smallest reliably-expired window."""
    token = issue_session(42)
    assert read_session(token) == 42          # valid right now

    monkeypatch.setattr(settings, "session_max_age_seconds", 0)
    time.sleep(1.2)
    assert read_session(token) is None


# =============================================================================
# Sign in and out
# =============================================================================


def test_login_sets_both_cookies_and_returns_the_user(client, users):
    response = client.post(
        "/api/auth/login",
        json={"email": "priya.nair@shreenm.com", "password": DEV_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "Priya Nair"

    cookies = response.cookies
    assert SESSION_COOKIE in cookies
    assert CSRF_COOKIE in cookies

    raw = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in raw if c.startswith(SESSION_COOKIE))
    csrf_cookie = next(c for c in raw if c.startswith(CSRF_COOKIE))

    # The session must not be readable by script; the CSRF token must be.
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in session_cookie.lower().replace("samesite=lax", "SameSite=lax")


def test_login_is_case_insensitive_on_email(client, users):
    response = client.post(
        "/api/auth/login",
        json={"email": "PRIYA.NAIR@SHREENM.COM", "password": DEV_PASSWORD},
    )
    assert response.status_code == 200


def test_a_wrong_password_is_refused(client, users):
    response = client.post(
        "/api/auth/login",
        json={"email": "priya.nair@shreenm.com", "password": "not the password"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == messages.BAD_CREDENTIALS


def test_an_unknown_address_gives_the_same_message(client):
    """The form must not reveal who has an account."""
    response = client.post(
        "/api/auth/login",
        json={"email": "nobody@shreenm.com", "password": "whatever"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == messages.BAD_CREDENTIALS


def test_a_deactivated_account_cannot_sign_in(client, db, users):
    user = users["vikram"]
    user.is_active = False
    db.commit()
    try:
        response = client.post(
            "/api/auth/login",
            json={"email": user.email, "password": DEV_PASSWORD},
        )
        assert response.status_code == 403
        assert user.email in response.json()["detail"]
    finally:
        user.is_active = True
        db.commit()


def test_logout_clears_the_session(client, users):
    logged_in = TestClient(app)
    logged_in.post(
        "/api/auth/login",
        json={"email": "priya.nair@shreenm.com", "password": DEV_PASSWORD},
    )
    assert logged_in.get("/api/me").status_code == 200

    csrf = logged_in.cookies.get(CSRF_COOKIE)
    out = logged_in.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
    assert out.status_code == 200
    assert out.json()["detail"] == messages.SIGNED_OUT

    logged_in.cookies.clear()
    assert logged_in.get("/api/me").status_code == 401


# =============================================================================
# Session enforcement
# =============================================================================


def test_no_session_is_401_everywhere(client, day):
    for method, path in [
        ("get", "/api/me"),
        ("get", "/api/rooms"),
        ("get", "/api/departments"),
        ("get", "/api/directory"),
        ("get", f"/api/availability?date={day.isoformat()}"),
        ("get", f"/api/availability/exit-cap?room=power&date={day.isoformat()}&entry=10:00"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path}"
        assert response.json()["detail"] == messages.ACTOR_NOT_IDENTIFIED


def test_a_session_for_a_deactivated_user_is_403(client, db, users, day):
    user = users["vikram"]
    user.is_active = False
    db.commit()
    try:
        response = client.get("/api/rooms", headers=headers_for(user))
        assert response.status_code == 403
        assert user.email in response.json()["detail"]
    finally:
        user.is_active = True
        db.commit()


# =============================================================================
# CSRF
# =============================================================================


def test_a_state_changing_request_without_a_csrf_token_is_refused(
    client, users, departments, day
):
    token = issue_session(users["priya"].id)
    response = client.post(
        "/api/bookings",
        json=booking_payload(
            room_id="spark",
            day=day,
            entry="10:00",
            exit_="11:00",
            department_id=departments["IT"].id,
            conducted_by=users["priya"].id,
        ),
        # Session present, CSRF cookie present, header missing.
        headers={"Cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={TEST_CSRF}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.CSRF_FAILED


def test_a_mismatched_csrf_token_is_refused(client, users, departments, day):
    token = issue_session(users["priya"].id)
    response = client.post(
        "/api/bookings",
        json=booking_payload(
            room_id="spark",
            day=day,
            entry="10:00",
            exit_="11:00",
            department_id=departments["IT"].id,
            conducted_by=users["priya"].id,
        ),
        headers={
            "Cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={TEST_CSRF}",
            CSRF_HEADER: "a-different-token",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.CSRF_FAILED


def test_reads_do_not_need_a_csrf_token(client, users):
    token = issue_session(users["priya"].id)
    response = client.get(
        "/api/rooms", headers={"Cookie": f"{SESSION_COOKIE}={token}"}
    )
    assert response.status_code == 200


# =============================================================================
# Section 9, enforced server-side
# =============================================================================


@pytest.fixture()
def booking(client, users, departments, rooms, day):
    """Booked by Rahul, conducted by Joseph, with Priya and Imran attending."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="switch",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["joseph"].id,
        attendee_ids=[users["priya"].id, users["imran"].id],
        title=f"{TEST_TITLE_PREFIX} roles",
    )
    assert created.status_code == 201, created.text
    return created.json()


# There are no privilege levels. Only the person who booked the room may cancel
# or change it - not reception, not an administrator, not the conductor.
CANCEL_ALLOWED = ["rahul"]
CANCEL_REFUSED = ["joseph", "reception", "admin", "priya", "imran", "neha", "sana"]


@pytest.mark.parametrize("handle", CANCEL_ALLOWED)
def test_only_the_booker_may_cancel(client, users, booking, handle):
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel", headers=headers_for(users[handle])
    )
    assert response.status_code == 200, f"{handle}: {response.text}"


@pytest.mark.parametrize("handle", CANCEL_REFUSED)
def test_everybody_else_is_refused_the_cancel(client, users, booking, handle):
    """Including the conductor, reception and the administrator."""
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel", headers=headers_for(users[handle])
    )
    assert response.status_code == 403, f"{handle}: {response.text}"
    assert response.json()["detail"] == messages.CANNOT_CANCEL.format(
        owner="Rahul Mehta"
    )


@pytest.mark.parametrize("handle", CANCEL_ALLOWED)
def test_only_the_booker_may_edit(client, users, booking, handle):
    response = client.patch(
        f"/api/bookings/{booking['id']}",
        json={"title": f"{TEST_TITLE_PREFIX} edited by {handle}"},
        headers=headers_for(users[handle]),
    )
    assert response.status_code == 200, f"{handle}: {response.text}"


@pytest.mark.parametrize("handle", CANCEL_REFUSED)
def test_everybody_else_is_refused_the_edit(client, users, booking, handle):
    response = client.patch(
        f"/api/bookings/{booking['id']}",
        json={"title": f"{TEST_TITLE_PREFIX} hijacked by {handle}"},
        headers=headers_for(users[handle]),
    )
    assert response.status_code == 403, f"{handle}: {response.text}"
    assert response.json()["detail"] == messages.CANNOT_EDIT.format(
        owner="Rahul Mehta"
    )


def test_there_are_no_roles_at_all(client, users, booking):
    """The role column is gone. The old privileged accounts are ordinary users."""
    for handle in ("reception", "admin"):
        me = client.get("/api/me", headers=headers_for(users[handle])).json()
        assert "role" not in me, me
        seen = client.get(
            f"/api/bookings/{booking['id']}", headers=headers_for(users[handle])
        ).json()
        assert seen["can_cancel"] is False, handle
        assert seen["can_edit"] is False, handle


def test_an_attendee_may_only_change_their_own_response(client, users, booking):
    ok = client.post(
        f"/api/bookings/{booking['id']}/response",
        json={"response": "DECLINED"},
        headers=headers_for(users["priya"]),
    )
    assert ok.status_code == 200
    priya = next(a for a in ok.json()["attendees"] if a["full_name"] == "Priya Nair")
    imran = next(a for a in ok.json()["attendees"] if a["full_name"] == "Imran Sheikh")
    assert priya["response_status"] == "DECLINED"
    assert imran["response_status"] == "PENDING"


def test_a_non_attendee_cannot_respond(client, users, booking):
    response = client.post(
        f"/api/bookings/{booking['id']}/response",
        json={"response": "ACCEPTED"},
        headers=headers_for(users["neha"]),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.NOT_AN_ATTENDEE


def test_permission_flags_match_what_the_server_enforces(client, users, booking):
    """The frontend hides what these say; the server refuses it either way."""
    expected = {
        "rahul": (True, True),      # booked it
        "joseph": (False, False),   # only running it
        "reception": (False, False),
        "admin": (False, False),
        "priya": (False, False),
        "neha": (False, False),
    }
    for handle, (can_cancel, can_edit) in expected.items():
        body = client.get(
            f"/api/bookings/{booking['id']}", headers=headers_for(users[handle])
        ).json()
        assert body["can_cancel"] is can_cancel, handle
        assert body["can_edit"] is can_edit, handle


def test_the_actor_comes_from_the_session_not_the_body(
    client, users, departments, rooms, day
):
    """A caller cannot book in somebody else's name by naming them in the body.

    conducted_by may be anyone in the directory, but booked_by is always the
    signed-in user, which is what section 9's cancel rights hang on.
    """
    created = post_booking(
        client,
        users["neha"],
        room_id="ignite",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201
    assert created.json()["booked_by"] == "Neha Kulkarni"
    assert created.json()["host"] == "Rahul Mehta"

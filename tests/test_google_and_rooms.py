"""Google Sign-In, room capacities, and the seven-day booking window."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import settings
from app.core import google_oauth, messages
from app.core.google_oauth import GoogleAuthError
from app.services import availability as av
from tests.conftest import booking_payload, headers_for


# =============================================================================
# Room capacities
# =============================================================================


EXPECTED = {"spark": 4, "power": 7, "pulse": 3, "ignite": 2, "switch": 2}


def test_rooms_carry_their_minimum_party_size(client, users):
    body = client.get("/api/rooms", headers=headers_for(users["priya"])).json()
    assert {r["id"]: r["min_people"] for r in body} == EXPECTED


def test_availability_carries_the_capacities_too(client, users, day):
    """The grid header shows them, so they travel with the grid payload."""
    body = client.get(
        f"/api/availability?date={day.isoformat()}",
        headers=headers_for(users["priya"]),
    ).json()
    assert {r["id"]: r["min_people"] for r in body["rooms"]} == EXPECTED


def test_the_capacity_note_reads_as_specified():
    assert (
        messages.ROOM_MIN_PEOPLE.format(room="Power", count=7)
        == "Power should only be selected if you are 7 or more people."
    )


def test_capacity_is_advisory_and_never_blocks_a_booking(
    client, users, departments, rooms, day
):
    """Two people may book Power. Nobody verifies a headcount, so refusing on
    one would be theatre - the note warns, the server allows."""
    response = client.post(
        "/api/bookings",
        json=booking_payload(
            room_id="power",
            day=day,
            entry="10:00",
            exit_="11:00",
            department_id=departments["Sales"].id,
            conducted_by=users["rahul"].id,
            attendee_ids=[users["priya"].id],
        ),
        headers=headers_for(users["rahul"]),
    )
    assert response.status_code == 201, response.text


# =============================================================================
# The seven-day window
# =============================================================================


def test_the_window_is_seven_days_including_today():
    """Standing in the office on the 1st you can book the 7th, not the 8th."""
    assert settings.max_advance_days == 7
    assert av.last_bookable_date() == date.today() + timedelta(days=6)


@pytest.mark.parametrize("offset", [0, 1, 5, 6])
def test_inside_the_window_is_allowed(offset):
    assert not av.is_too_far_ahead(date.today() + timedelta(days=offset))


@pytest.mark.parametrize("offset", [7, 8, 30])
def test_outside_the_window_is_refused(offset):
    assert av.is_too_far_ahead(date.today() + timedelta(days=offset))


def test_availability_publishes_the_window(client, users, day):
    """So the calendar does not duplicate the arithmetic."""
    body = client.get(
        f"/api/availability?date={day.isoformat()}",
        headers=headers_for(users["priya"]),
    ).json()
    assert body["last_bookable_date"] == av.last_bookable_date().isoformat()
    assert body["max_advance_days"] == 7
    assert body["closed_weekdays"] == [6]      # Sunday


# =============================================================================
# Google Sign-In
# =============================================================================


def test_google_is_not_configured_out_of_the_box():
    assert google_oauth.is_configured() is False


def test_status_endpoint_tells_the_page_to_hide_the_button(client):
    """No session needed: the login page asks before anybody has signed in."""
    body = client.get("/api/auth/google/status").json()
    assert body["configured"] is False
    assert body["domain"] == "shreenm.com"
    assert body["start_url"] == "/api/auth/google/start"


def test_start_refuses_politely_while_unconfigured(client):
    response = client.get("/api/auth/google/start", follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["detail"] == messages.GOOGLE_NOT_CONFIGURED


@pytest.fixture()
def google_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "test-secret")
    monkeypatch.setattr(
        settings, "google_redirect_uri", "http://localhost:8000/api/auth/google/callback"
    )


def test_start_redirects_to_google_with_pkce_and_state(client, google_configured):
    response = client.get("/api/auth/google/start", follow_redirects=False)
    assert response.status_code == 307

    target = urlparse(response.headers["location"])
    assert target.netloc == "accounts.google.com"
    q = parse_qs(target.query)

    assert q["client_id"] == ["test-client-id.apps.googleusercontent.com"]
    assert q["response_type"] == ["code"]
    assert set(q["scope"][0].split()) == {"openid", "email", "profile"}
    # PKCE: Google only ever sees the hash of the verifier.
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"][0]
    assert q["state"][0]
    # Steers the account chooser at the work domain.
    assert q["hd"] == ["shreenm.com"]

    # The state and verifier come back in a signed, HttpOnly cookie.
    cookie = next(
        c for c in response.headers.get_list("set-cookie")
        if c.startswith(google_oauth.OAUTH_COOKIE)
    )
    assert "HttpOnly" in cookie


def test_the_oauth_cookie_round_trips(google_configured):
    _, cookie = google_oauth.build_authorisation_url()
    state, verifier = google_oauth.read_oauth_cookie(cookie)
    assert state and verifier


def test_a_tampered_oauth_cookie_is_refused(google_configured):
    _, cookie = google_oauth.build_authorisation_url()
    with pytest.raises(GoogleAuthError) as excinfo:
        google_oauth.read_oauth_cookie(cookie[:-3] + "aaa")
    assert excinfo.value.detail == messages.GOOGLE_STATE_MISMATCH


def test_a_missing_oauth_cookie_is_refused():
    with pytest.raises(GoogleAuthError) as excinfo:
        google_oauth.read_oauth_cookie(None)
    assert excinfo.value.detail == messages.GOOGLE_STATE_MISMATCH


def test_pkce_challenge_is_the_s256_of_the_verifier():
    import base64, hashlib

    verifier = google_oauth._code_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    assert google_oauth._code_challenge(verifier) == expected
    assert "=" not in google_oauth._code_challenge(verifier)


def test_callback_without_a_code_returns_to_the_login_page(client, google_configured):
    response = client.get("/api/auth/google/callback", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login.html?error=")


def test_callback_with_a_mismatched_state_is_refused(client, google_configured):
    """A login-CSRF attempt: the state does not match the cookie."""
    started = client.get("/api/auth/google/start", follow_redirects=False)
    cookie = started.cookies[google_oauth.OAUTH_COOKIE]

    response = client.get(
        "/api/auth/google/callback?code=whatever&state=not-the-right-state",
        headers={"Cookie": f"{google_oauth.OAUTH_COOKIE}={cookie}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_the_domain_rule_is_stated_in_the_message():
    detail = messages.GOOGLE_WRONG_DOMAIN.format(
        email="someone@gmail.com", domain="shreenm.com"
    )
    assert "someone@gmail.com" in detail
    assert "shreenm.com" in detail

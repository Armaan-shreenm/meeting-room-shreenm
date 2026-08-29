"""The shipped default: no sign-in at all.

NM Meet is internal, so it asks nobody to sign in. Whoever the booking form
names as host is who the booking belongs to - that is the whole of the
identity. There is no login page and no session.

The consequence is deliberate and worth stating plainly: **anyone can cancel
anyone's booking.** Without a sign-in the server has no way to tell one person
from another, so it does not pretend to. The ownership rule is still written,
still tested (in the rest of the suite, which runs with sign-in on) and comes
back the day Google Sign-In is switched on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import messages
from app.models import Booking, BookingStatus
from tests.conftest import booking_payload, headers_for

pytestmark = pytest.mark.usefixtures("open_access")


def post(client, **kwargs):
    """No headers: nobody is signed in."""
    return client.post("/api/bookings", json=booking_payload(**kwargs))


# =============================================================================
# Everything is reachable without signing in
# =============================================================================


def test_the_shipped_default_asks_for_no_sign_in():
    assert settings.sign_in_required is False


def test_there_is_no_login_page():
    """It was deleted, not hidden."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "static"
    assert not (static / "login.html").exists()


@pytest.mark.parametrize(
    "path",
    ["/api/rooms", "/api/departments", "/api/directory"],
)
def test_reference_data_is_open(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text


def test_the_grid_is_open(client, day):
    response = client.get(f"/api/availability?date={day.isoformat()}")
    assert response.status_code == 200, response.text
    assert len(response.json()["rooms"]) == 5


def test_the_exit_cap_is_open(client, day):
    response = client.get(
        f"/api/availability/exit-cap?room=power&date={day.isoformat()}&entry=10:00"
    )
    assert response.status_code == 200, response.text


# =============================================================================
# The host named in the form owns the booking
# =============================================================================


def test_booking_needs_no_session(client, users, departments, rooms, day):
    response = post(
        client,
        room_id="power",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert response.status_code == 201, response.text


def test_the_host_becomes_the_booker(client, db, users, departments, rooms, day):
    """There is no signed-in user, so the form's host is who it belongs to."""
    response = post(
        client,
        room_id="pulse",
        day=day,
        entry="11:00",
        exit_="12:00",
        department_id=departments["Finance"].id,
        conducted_by=users["joseph"].id,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["host"] == "Joseph Dsouza"
    assert body["booked_by"] == "Joseph Dsouza"

    row = db.scalar(select(Booking).where(Booking.id == body["id"]))
    assert row.booked_by == row.conducted_by == users["joseph"].id


def test_a_booking_without_a_host_is_refused(client, departments, rooms, day):
    """With no session there is no fallback, so the field is genuinely required."""
    payload = booking_payload(
        room_id="ignite",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["IT"].id,
        conducted_by=1,
    )
    payload["conducted_by"] = None
    response = client.post("/api/bookings", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == messages.CONDUCTOR_REQUIRED


def test_the_department_is_still_required(client, users, rooms, day):
    payload = booking_payload(
        room_id="ignite",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=1,
        conducted_by=users["priya"].id,
    )
    payload["department_id"] = None
    response = client.post("/api/bookings", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == messages.DEPARTMENT_REQUIRED


# =============================================================================
# Anyone can cancel - stated, not hidden
# =============================================================================


def test_anyone_can_cancel_because_nobody_signs_in(
    client, db, users, departments, rooms, day
):
    created = post(
        client,
        room_id="switch",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    booking_id = created.json()["id"]

    # No session, and the booking is Rahul's. It cancels anyway: there is
    # nobody to compare against. This is the cost of having no sign-in.
    response = client.post(f"/api/bookings/{booking_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"

    row = db.scalar(select(Booking).where(Booking.id == booking_id))
    assert row.status == BookingStatus.CANCELLED


def test_the_panel_offers_cancel_to_everyone(client, users, departments, rooms, day):
    created = post(
        client,
        room_id="spark",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    detail = client.get(f"/api/bookings/{created.json()['id']}").json()
    assert detail["can_cancel"] is True
    assert detail["can_edit"] is True
    # Nobody is signed in, so nobody has a place to accept or decline.
    assert detail["can_respond"] is False


def test_details_can_be_edited_without_a_session(
    client, users, departments, rooms, day
):
    created = post(
        client,
        room_id="spark",
        day=day,
        entry="12:00",
        exit_="13:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    response = client.patch(
        f"/api/bookings/{created.json()['id']}",
        json={"department_id": departments["HR"].id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["department"] == "HR"


def test_room_date_and_time_are_still_immutable(
    client, users, departments, rooms, day
):
    """Open access does not mean anything goes."""
    created = post(
        client,
        room_id="spark",
        day=day,
        entry="15:00",
        exit_="16:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    response = client.patch(
        f"/api/bookings/{created.json()['id']}", json={"entry": "10:00"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.IMMUTABLE_FIELDS


# =============================================================================
# The rules that do not depend on who you are still hold
# =============================================================================


def test_double_booking_is_still_impossible(
    client, users, departments, rooms, day
):
    first = post(
        client,
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert first.status_code == 201

    clash = post(
        client,
        room_id="power",
        day=day,
        entry="14:00",
        exit_="16:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert clash.status_code == 409
    assert clash.json()["detail"].startswith(
        "Power was booked by someone else a moment ago."
    )


def test_office_hours_still_apply(client, users, departments, rooms, day):
    response = post(
        client,
        room_id="power",
        day=day,
        entry="19:00",
        exit_="20:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.outside_hours()


def test_sundays_are_still_closed(client, users, departments, rooms):
    from tests.conftest import next_sunday

    response = post(
        client,
        room_id="power",
        day=next_sunday(offset=1),
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.OFFICE_CLOSED


# =============================================================================
# The sign-in machinery is dormant, not deleted
# =============================================================================


def test_google_endpoints_are_still_there(client):
    """Switching Google on is configuration, not code."""
    body = client.get("/api/auth/google/status").json()
    assert body["configured"] is False
    assert body["start_url"] == "/api/auth/google/start"


def test_password_login_still_works_underneath(client, users):
    """Kept as the fallback for the day sign-in is switched on."""
    response = client.post(
        "/api/auth/login",
        json={"email": "priya.nair@shreenm.com", "password": "nmmeet-dev"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "Priya Nair"


def test_turning_sign_in_on_restores_the_rules(
    client, users, departments, rooms, day, monkeypatch
):
    """One setting brings back the session requirement."""
    created = post(
        client,
        room_id="ignite",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201

    monkeypatch.setattr(settings, "sign_in_required", True)

    # Now an unsigned request is refused...
    assert client.get("/api/rooms").status_code == 401
    # ...and Rahul cannot be cancelled by an anonymous caller.
    assert client.post(f"/api/bookings/{created.json()['id']}/cancel").status_code == 401
    # ...but Priya, signed in, is refused for the right reason: it is not hers.
    refused = client.post(
        f"/api/bookings/{created.json()['id']}/cancel",
        headers=headers_for(users["priya"]),
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == messages.CANNOT_CANCEL.format(
        owner="Rahul Mehta"
    )

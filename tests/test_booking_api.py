"""Phase 2 — availability and booking creation.

Covers the specification's own test cases that are backend-testable at this
stage: T01, T02, T03, T04, T05, T06, T07, T08 and T15, plus the closed-day,
horizon and timezone rules.

Every rejection is asserted against the exact string from app.core.messages, not
a status code alone. A 400 carrying "Booking failed" would pass a status-only
assertion and would still be a bug.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.database import SessionLocal
from app.main import app
from app.models import Booking, Holiday
from app.services import availability as av
from tests.conftest import (
    TEST_TITLE_PREFIX,
    beyond_horizon,
    booking_payload,
    headers_for,
    next_sunday,
    working_day,
)


def post_booking(client, user, **kwargs):
    return client.post(
        "/api/bookings", json=booking_payload(**kwargs), headers=headers_for(user)
    )


# =============================================================================
# Reference endpoints
# =============================================================================


def test_rooms_endpoint_returns_five_in_display_order(client):
    response = client.get("/api/rooms")
    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body] == ["spark", "power", "pulse", "ignite", "switch"]
    # The frontend colours each room from this variable.
    assert body[0]["colour_var"] == "--spark"


def test_departments_endpoint(client):
    response = client.get("/api/departments")
    assert response.status_code == 200
    names = {d["name"] for d in response.json()}
    assert {"Sales", "HR", "Finance", "Operations", "IT", "Marketing"} <= names


def test_directory_returns_only_active_members(client):
    response = client.get("/api/directory")
    assert response.status_code == 200
    body = response.json()
    assert any(u["email"] == "priya.nair@shreenm.com" for u in body)
    assert all(u["full_name"] for u in body)


# =============================================================================
# T-01 / T-02 — availability is per room, never global
# =============================================================================


def test_T01_booked_window_is_blocked_on_that_room(
    client, users, departments, rooms, day
):
    created = post_booking(
        client,
        users["rahul"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text

    availability = client.get(f"/api/availability?date={day.isoformat()}").json()
    power = next(r for r in availability["rooms"] if r["id"] == "power")

    assert len(power["bookings"]) == 1
    assert power["bookings"][0]["entry"] == "13:00"
    assert power["bookings"][0]["exit"] == "15:00"

    # 13:00, 13:30, 14:00 and 14:30 are gone from Power's 22 slots.
    assert power["free_slot_count"] == len(av.slot_starts()) - 4


def test_T02_other_rooms_are_untouched(client, users, departments, rooms, day):
    post_booking(
        client,
        users["rahul"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )

    availability = client.get(f"/api/availability?date={day.isoformat()}").json()

    for room in availability["rooms"]:
        if room["id"] == "power":
            continue
        assert room["bookings"] == []
        assert room["free_slot_count"] == len(av.slot_starts())

    # And the same window really is bookable on another room.
    other = post_booking(
        client,
        users["sana"],
        room_id="pulse",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Operations"].id,
        conducted_by=users["sana"].id,
    )
    assert other.status_code == 201, other.text


# =============================================================================
# T-03 / T-04 — back to back is allowed at both edges
# =============================================================================


@pytest.mark.parametrize(
    ("entry", "exit_", "why"),
    [
        ("12:00", "13:00", "ends exactly when the other starts"),
        ("15:00", "16:00", "starts exactly when the other ends"),
    ],
)
def test_T03_T04_back_to_back_succeeds(
    client, users, departments, rooms, day, entry, exit_, why
):
    anchor = post_booking(
        client,
        users["rahul"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    assert anchor.status_code == 201

    adjacent = post_booking(
        client,
        users["neha"],
        room_id="power",
        day=day,
        entry=entry,
        exit_=exit_,
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert adjacent.status_code == 201, f"{why}: {adjacent.text}"


def test_overlap_is_refused(client, users, departments, rooms, day):
    post_booking(
        client,
        users["rahul"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )

    clash = post_booking(
        client,
        users["neha"],
        room_id="power",
        day=day,
        entry="14:00",
        exit_="16:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert clash.status_code in (400, 409)
    detail = clash.json()["detail"]
    assert "Power" in detail
    assert detail not in ("Invalid selection", "Booking failed")


# =============================================================================
# T-05 — exit cap
# =============================================================================


def test_T05_exit_cap_offers_only_1pm_for_a_1230_entry(
    client, users, departments, rooms, day
):
    """Section 4's worked example, exactly."""
    post_booking(
        client,
        users["rahul"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )

    response = client.get(
        f"/api/availability/exit-cap?room=power&date={day.isoformat()}&entry=12:30"
    )
    assert response.status_code == 200
    body = response.json()

    assert body["exit_cap"] == "13:00"
    assert body["limited_by"] == "next_booking"
    assert body["next_booking_entry"] == "13:00"
    # "the exit dial offers 13:00 only"
    assert body["options"] == ["13:00"]


def test_exit_cap_without_a_following_booking_is_four_hours(
    client, rooms, day
):
    response = client.get(
        f"/api/availability/exit-cap?room=pulse&date={day.isoformat()}&entry=09:00"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["exit_cap"] == "13:00"  # 09:00 + 4h
    assert body["limited_by"] == "max_length"


def test_exit_cap_near_closing_is_capped_by_closing_time(client, rooms, day):
    response = client.get(
        f"/api/availability/exit-cap?room=pulse&date={day.isoformat()}&entry=18:00"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["exit_cap"] == "20:00"
    assert body["limited_by"] == "closing_time"


def test_exit_running_into_the_next_booking_is_refused_as_a_clash(
    client, users, departments, rooms, day
):
    """An exit past the cap overlaps the next booking, so it is a clash.

    Section 4 describes the dial stopping at 13:00; the server sees 12:30-13:30
    as an overlap and answers with section 5's wording rather than inventing a
    message the section 10 table does not contain.
    """
    post_booking(
        client,
        users["rahul"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )

    over = post_booking(
        client,
        users["neha"],
        room_id="power",
        day=day,
        entry="12:30",
        exit_="13:30",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert over.status_code == 409
    detail = over.json()["detail"]
    assert detail.startswith("Power was booked by someone else a moment ago.")
    assert "still free from 12:30 pm to 1:30 pm" in detail
    # Spec writes times as "1:30 pm", never "13:30".
    assert "13:30" not in detail


# =============================================================================
# T-06 — concurrency. Real threads, shared barrier, no mocking.
# =============================================================================


def test_T06_simultaneous_identical_bookings_yield_one_201_and_one_409(
    users, departments, rooms, day
):
    """Two people press Confirm in the same instant. One wins, one is told why."""
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    payload = booking_payload(
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        title=f"{TEST_TITLE_PREFIX} race",
    )

    def attempt(actor):
        # A client per thread: separate connection, separate session.
        with TestClient(app, raise_server_exceptions=False) as thread_client:
            barrier.wait(timeout=20)
            response = thread_client.post(
                "/api/bookings", json=payload, headers=headers_for(actor)
            )
            with lock:
                results.append((response.status_code, response.json()))

    threads = [
        threading.Thread(target=attempt, args=(users["rahul"],)),
        threading.Thread(target=attempt, args=(users["neha"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a booking thread hung"

    assert len(results) == 2
    statuses = sorted(status for status, _ in results)
    assert statuses == [201, 409], f"expected one win and one loss, got {results}"

    loser = next(body for status, body in results if status == 409)
    detail = loser["detail"]

    # Section 5's wording, naming this room and the rooms still free.
    assert detail.startswith("Power was booked by someone else a moment ago.")
    assert "still free from 1 pm to 3 pm" in detail
    assert "13:00" not in detail

    # The free list is computed, so it names the other four rooms.
    for name in ("Spark", "Pulse", "Ignite", "Switch"):
        assert name in detail
    assert set(loser["free_rooms"]) == {"Spark", "Pulse", "Ignite", "Switch"}

    # Exactly one row reached the database.
    with SessionLocal() as session:
        rows = session.scalars(
            select(Booking).where(Booking.title == f"{TEST_TITLE_PREFIX} race")
        ).all()
        assert len(rows) == 1


def test_all_rooms_taken_names_the_first_to_free_up(
    client, users, departments, rooms, day
):
    """Section 10's other clash message, with both values computed."""
    for room_id in ("spark", "power", "pulse", "ignite", "switch"):
        created = post_booking(
            client,
            users["rahul"],
            room_id=room_id,
            day=day,
            entry="13:00",
            exit_="15:00",
            department_id=departments["Sales"].id,
            conducted_by=users["rahul"].id,
        )
        assert created.status_code == 201, created.text

    sixth = post_booking(
        client,
        users["neha"],
        room_id="power",
        day=day,
        entry="13:00",
        exit_="15:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert sixth.status_code == 409
    detail = sixth.json()["detail"]

    assert detail.startswith("All five rooms are booked between 1 pm and 3 pm.")
    assert "The first free room is" in detail
    assert "at 3 pm." in detail
    assert sixth.json()["free_rooms"] == []


# =============================================================================
# T-07, T-08, T-15 — the validation table
# =============================================================================


def test_T07_exit_before_entry(client, users, departments, rooms, day):
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="15:00",
        exit_="14:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.EXIT_NOT_AFTER_ENTRY


def test_exit_equal_to_entry_is_also_refused(
    client, users, departments, rooms, day
):
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="14:00",
        exit_="14:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.EXIT_NOT_AFTER_ENTRY


def test_T08_outside_working_hours(client, users, departments, rooms, day):
    """19:00 to 21:00 runs past the 20:00 close."""
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="19:00",
        exit_="21:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.OUTSIDE_HOURS


def test_before_opening_is_refused(client, users, departments, rooms, day):
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="08:00",
        exit_="09:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.OUTSIDE_HOURS


def test_T15_quarter_past_is_not_a_slot_boundary(
    client, users, departments, rooms, day
):
    """Minutes snap to :00 and :30 — a 14:15 entry cannot be booked."""
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="14:15",
        exit_="15:15",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.NOT_ON_SLOT_BOUNDARY


def test_shorter_than_thirty_minutes(client, users, departments, rooms, day):
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="14:00",
        exit_="14:15",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.TOO_SHORT


def test_longer_than_four_hours(client, users, departments, rooms, day):
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="09:00",
        exit_="14:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.TOO_LONG


def test_missing_department(client, users, rooms, day):
    payload = booking_payload(
        room_id="spark",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=1,
        conducted_by=users["priya"].id,
    )
    payload["department_id"] = None
    response = client.post(
        "/api/bookings", json=payload, headers=headers_for(users["priya"])
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.DEPARTMENT_REQUIRED


def test_missing_conductor(client, users, departments, rooms, day):
    payload = booking_payload(
        room_id="spark",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    payload["conducted_by"] = None
    response = client.post(
        "/api/bookings", json=payload, headers=headers_for(users["priya"])
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.CONDUCTOR_REQUIRED


def test_unknown_room(client, users, departments, day):
    response = post_booking(
        client,
        users["priya"],
        room_id="atrium",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == messages.UNKNOWN_ROOM.format(room="atrium")


# =============================================================================
# Closed days and the horizon
# =============================================================================


def test_sunday_is_closed(client, users, departments, rooms):
    sunday = next_sunday(offset=1)
    assert sunday.weekday() == 6

    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=sunday,
        entry="14:00",
        exit_="15:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.OFFICE_CLOSED


def test_availability_reports_sunday_as_closed(client):
    sunday = next_sunday(offset=1)
    body = client.get(f"/api/availability?date={sunday.isoformat()}").json()
    assert body["closed"] is True
    assert body["closed_reason"] == "weekly_closure"
    # The grid still gets all five rooms so it can render itself greyed out.
    assert len(body["rooms"]) == 5
    assert all(r["free_slot_count"] == 0 for r in body["rooms"])


def test_declared_holiday_is_closed(client, db, users, departments, rooms):
    holiday_date = working_day(45)
    db.add(Holiday(date=holiday_date, name=f"{TEST_TITLE_PREFIX} Founders Day"))
    db.commit()

    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=holiday_date,
        entry="14:00",
        exit_="15:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.OFFICE_CLOSED

    body = client.get(f"/api/availability?date={holiday_date.isoformat()}").json()
    assert body["closed"] is True
    assert body["closed_reason"] == "holiday"
    assert body["closed_detail"] == f"{TEST_TITLE_PREFIX} Founders Day"


def test_past_date_is_refused(client, users, departments, rooms):
    yesterday = date.today() - timedelta(days=1)
    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=yesterday,
        entry="14:00",
        exit_="15:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.DATE_PAST


def test_ninety_one_days_ahead_is_refused(client, users, departments, rooms):
    far = beyond_horizon()
    assert (far - date.today()).days > settings.max_advance_days

    response = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=far,
        entry="14:00",
        exit_="15:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.TOO_FAR_AHEAD.format(
        days=settings.max_advance_days
    )


# =============================================================================
# Timezone — local date drives everything, never the UTC date
# =============================================================================


def test_stored_times_are_utc_and_the_local_date_is_kept(
    client, db, users, departments, rooms, day
):
    """A 09:00 Mumbai booking is stored as 03:30 UTC on the same date."""
    created = post_booking(
        client,
        users["priya"],
        room_id="spark",
        day=day,
        entry="09:00",
        exit_="10:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert created.status_code == 201

    row = db.scalar(select(Booking).where(Booking.id == created.json()["id"]))
    utc_entry = row.entry_time.astimezone(timezone.utc)

    assert (utc_entry.hour, utc_entry.minute) == (3, 30)
    assert row.booking_date == day
    assert timeutil.minutes_from_midnight(row.entry_time) == 9 * 60

    # And the API hands back local strings, never a UTC timestamp.
    assert created.json()["entry"] == "09:00"


def test_local_date_differs_from_utc_date_late_in_the_evening(monkeypatch):
    """At 19:00 UTC it is already tomorrow in Mumbai.

    Every "is this in the past" decision uses the branch's today. If it used the
    server's UTC today, a booking made late in the Mumbai evening for the day
    that has already started there would be refused as past.
    """
    frozen_utc = datetime(2026, 9, 10, 19, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(timeutil, "now_utc", lambda: frozen_utc)
    monkeypatch.setattr(
        timeutil, "now_local", lambda: frozen_utc.astimezone(settings.timezone)
    )

    assert frozen_utc.date() == date(2026, 9, 10)          # the UTC day
    assert timeutil.local_today() == date(2026, 9, 11)     # the Mumbai day

    with SessionLocal() as session:
        # 11 September 2026 is a Friday, and it is "today" at the branch, so it
        # must not be reported as past.
        status = av.day_status(session, date(2026, 9, 11))
        assert status.closed is False

        # 10 September, already yesterday in Mumbai, is past.
        assert av.day_status(session, date(2026, 9, 10)).reason == "past"


def test_booking_date_is_derived_not_taken_from_the_payload(
    client, db, users, departments, rooms, day
):
    """booking_date always comes from entry_time via booking_date_for()."""
    created = post_booking(
        client,
        users["priya"],
        room_id="ignite",
        day=day,
        entry="17:00",
        exit_="19:00",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    assert created.status_code == 201

    row = db.scalar(select(Booking).where(Booking.id == created.json()["id"]))
    assert row.booking_date == timeutil.booking_date_for(row.entry_time)


# =============================================================================
# Detail payload
# =============================================================================


def test_booking_detail_carries_everything_the_panel_shows(
    client, users, departments, rooms, day
):
    created = post_booking(
        client,
        users["rahul"],
        room_id="switch",
        day=day,
        entry="10:00",
        exit_="11:30",
        department_id=departments["Finance"].id,
        conducted_by=users["joseph"].id,
        attendee_ids=[users["priya"].id, users["aditi"].id],
        title=f"{TEST_TITLE_PREFIX} Board meeting",
        reception_note="Tea for 8",
    )
    assert created.status_code == 201, created.text
    booking_id = created.json()["id"]

    detail = client.get(
        f"/api/bookings/{booking_id}", headers=headers_for(users["rahul"])
    )
    assert detail.status_code == 200
    body = detail.json()

    assert body["room_name"] == "Switch"
    assert body["colour_var"] == "--switch"
    assert body["entry"] == "10:00"
    assert body["exit"] == "11:30"
    assert body["title"] == f"{TEST_TITLE_PREFIX} Board meeting"
    assert body["department"] == "Finance"
    assert body["host"] == "Joseph Dsouza"
    assert body["booked_by"] == "Rahul Mehta"
    assert body["reception_note"] == "Tea for 8"
    assert body["status"] == "CONFIRMED"
    assert {a["full_name"] for a in body["attendees"]} == {
        "Priya Nair",
        "Aditi Shah",
    }
    assert all(a["response_status"] == "PENDING" for a in body["attendees"])

    # The booker may cancel and edit their own booking.
    assert body["can_cancel"] is True
    assert body["can_edit"] is True


def test_attendee_cannot_cancel_or_edit(client, users, departments, rooms, day):
    """Section 9: an attendee may only decline."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="switch",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        attendee_ids=[users["imran"].id],
    )
    booking_id = created.json()["id"]

    seen_by_attendee = client.get(
        f"/api/bookings/{booking_id}", headers=headers_for(users["imran"])
    ).json()
    assert seen_by_attendee["can_cancel"] is False
    assert seen_by_attendee["can_edit"] is False


def test_reception_may_cancel_anyones_booking(
    client, users, departments, rooms, day
):
    created = post_booking(
        client,
        users["rahul"],
        room_id="switch",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
    )
    booking_id = created.json()["id"]

    for handle in ("reception", "admin"):
        body = client.get(
            f"/api/bookings/{booking_id}", headers=headers_for(users[handle])
        ).json()
        assert body["can_cancel"] is True, handle


def test_unknown_booking_id_is_polite(client, users):
    response = client.get(
        "/api/bookings/not-a-uuid", headers=headers_for(users["priya"])
    )
    assert response.status_code == 404
    assert response.json()["detail"] == messages.UNKNOWN_BOOKING


def test_title_defaults_to_meeting_when_blank(
    client, db, users, departments, rooms, day
):
    """Spec field 8: defaults to "Meeting" if left blank."""
    payload = booking_payload(
        room_id="ignite",
        day=day,
        entry="16:00",
        exit_="16:30",
        department_id=departments["IT"].id,
        conducted_by=users["priya"].id,
    )
    payload["title"] = "   "
    response = client.post(
        "/api/bookings", json=payload, headers=headers_for(users["priya"])
    )
    assert response.status_code == 201
    assert response.json()["title"] == settings.default_meeting_title

    # Clean up by hand: the blank title escapes the TEST_TITLE_PREFIX sweep.
    db.execute(
        Booking.__table__.delete().where(Booking.id == response.json()["id"])
    )
    db.commit()

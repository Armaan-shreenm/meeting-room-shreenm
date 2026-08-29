"""Phase 3 — cancel, edit, no-show, attendee response and notifications.

Covers T09, T10, T11, T12 and T13 from spec section 12, plus the no-show timing
rule and the immediate re-bookability of a released window.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.models import (
    AuditLog,
    Booking,
    BookingAttendee,
    BookingStatus,
    NotificationEvent,
    NotificationLog,
    NotificationStatus,
)
from app.services import notifications
from tests.conftest import (
    TEST_TITLE_PREFIX,
    booking_payload,
    headers_for,
    working_day,
)


def post_booking(client, user, **kwargs):
    return client.post(
        "/api/bookings", json=booking_payload(**kwargs), headers=headers_for(user)
    )


def logs_for(db, booking_id) -> list[NotificationLog]:
    return list(
        db.scalars(
            select(NotificationLog)
            .where(NotificationLog.booking_id == uuid.UUID(str(booking_id)))
            .order_by(NotificationLog.created_at, NotificationLog.recipient)
        ).all()
    )


def audit_for(db, booking_id) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == str(booking_id))
            .order_by(AuditLog.created_at)
        ).all()
    )


@pytest.fixture()
def booking(client, users, departments, rooms, day):
    """A confirmed Switch booking by Rahul with two attendees."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="switch",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        attendee_ids=[users["priya"].id, users["aditi"].id],
        title=f"{TEST_TITLE_PREFIX} Sales review",
    )
    assert created.status_code == 201, created.text
    return created.json()


# =============================================================================
# T-09 — notifications on booking
# =============================================================================


def test_T09_two_attendees_produce_five_notifications(db, booking):
    """Two attendees, the conductor, reception and the Mumbai group.

    Rahul is both booker and conductor here, so he is one recipient, not two —
    which is exactly the five the specification's test case counts.
    """
    rows = logs_for(db, booking["id"])
    assert len(rows) == 5, [r.recipient for r in rows]
    assert {r.event for r in rows} == {NotificationEvent.BOOKED}

    recipients = {r.recipient for r in rows}
    assert recipients == {
        "priya.nair@shreenm.com",
        "aditi.shah@shreenm.com",
        "rahul.mehta@shreenm.com",
        settings.reception_email,
        settings.mumbai_group_email,
    }


def test_branch_group_is_logged_but_deferred_to_the_daily_summary(db, booking):
    """D-01: the Mumbai list gets a daily 8 am summary, not a mail per booking."""
    rows = {r.recipient: r for r in logs_for(db, booking["id"])}

    branch = rows[settings.mumbai_group_email]
    assert branch.status == NotificationStatus.QUEUED
    assert branch.sent_at is None

    for email, row in rows.items():
        if email == settings.mumbai_group_email:
            continue
        assert row.status == NotificationStatus.SENT, email
        assert row.sent_at is not None


def test_booker_and_conductor_are_both_notified_when_different(
    client, db, users, departments, rooms, day
):
    """Section 8 lists them separately; they only collapse when it is one person."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["joseph"].id,
        attendee_ids=[users["priya"].id],
    )
    assert created.status_code == 201

    recipients = {r.recipient for r in logs_for(db, created.json()["id"])}
    assert "rahul.mehta@shreenm.com" in recipients   # booker
    assert "joseph.dsouza@shreenm.com" in recipients  # conductor
    assert len(recipients) == 5


def test_message_contains_everything_section_8_requires(db, booking):
    """Room, date, times, title, department, conductor, attendees, note, link."""
    row = db.scalar(select(Booking).where(Booking.id == uuid.UUID(booking["id"])))
    recipient = notifications.Recipient(
        email="someone@shreenm.com", name="Someone", kind=notifications.ATTENDEE
    )
    message = notifications.render(row, NotificationEvent.BOOKED, recipient)

    for fragment in (
        "Switch",
        "10 am to 11 am",
        f"{TEST_TITLE_PREFIX} Sales review",
        "Sales",
        "Rahul Mehta",
        "Priya Nair",
        "Aditi Shah",
        "View or cancel this booking:",
        str(row.id),
    ):
        assert fragment in message.body, fragment

    # Times are written the way the spec writes them.
    assert "10:00" not in message.body
    # No video-conference link is ever created.
    assert "meet.google.com" not in message.body.lower()


def test_audit_row_written_on_create(db, booking):
    rows = audit_for(db, booking["id"])
    assert [r.action for r in rows] == ["CREATED"]
    assert rows[0].after["status"] == "CONFIRMED"
    assert rows[0].before is None


# =============================================================================
# T-10 — cancelling frees the window and notifies
# =============================================================================


def test_T10_cancel_frees_the_window_and_notifies(
    client, db, users, booking, day
):
    cancelled = client.post(
        f"/api/bookings/{booking['id']}/cancel",
        headers=headers_for(users["rahul"]),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    # The window is free for everyone, immediately.
    availability = client.get(f"/api/availability?date={day.isoformat()}", headers=headers_for(users["priya"])).json()
    switch = next(r for r in availability["rooms"] if r["id"] == "switch")
    assert switch["bookings"] == []

    # All five recipient groups are told.
    cancel_rows = [
        r
        for r in logs_for(db, booking["id"])
        if r.event == NotificationEvent.CANCELLED
    ]
    assert len(cancel_rows) == 5

    # Never deleted — kept for the audit record.
    row = db.scalar(select(Booking).where(Booking.id == uuid.UUID(booking["id"])))
    assert row is not None
    assert row.status == BookingStatus.CANCELLED

    assert [r.action for r in audit_for(db, booking["id"])] == ["CREATED", "CANCELLED"]


def test_cancelled_window_is_immediately_rebookable(
    client, users, departments, rooms, booking, day
):
    client.post(
        f"/api/bookings/{booking['id']}/cancel",
        headers=headers_for(users["rahul"]),
    )

    again = post_booking(
        client,
        users["neha"],
        room_id="switch",
        day=day,
        entry="10:00",
        exit_="11:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert again.status_code == 201, again.text


def test_cancelling_twice_is_refused(client, users, booking):
    first = client.post(
        f"/api/bookings/{booking['id']}/cancel",
        headers=headers_for(users["rahul"]),
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/bookings/{booking['id']}/cancel",
        headers=headers_for(users["rahul"]),
    )
    assert second.status_code == 400
    assert second.json()["detail"] == messages.ALREADY_CANCELLED


# =============================================================================
# T-11 / T-12 — who may cancel
# =============================================================================


@pytest.mark.parametrize("handle", ["reception", "admin"])
def test_T11_reception_and_admin_may_cancel_anyones_booking(
    client, users, booking, handle
):
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel", headers=headers_for(users[handle])
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"


def test_T12_an_attendee_cannot_cancel(client, users, booking):
    """Section 9: an attendee may only decline."""
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel", headers=headers_for(users["priya"])
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.CANNOT_CANCEL.format(
        owner="Rahul Mehta"
    )

    # And the frontend is told not to offer it.
    detail = client.get(
        f"/api/bookings/{booking['id']}", headers=headers_for(users["priya"])
    ).json()
    assert detail["can_cancel"] is False
    assert detail["can_respond"] is True


def test_an_unrelated_employee_cannot_cancel(client, users, booking):
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel", headers=headers_for(users["imran"])
    )
    assert response.status_code == 403


def test_the_conductor_may_cancel_even_if_someone_else_booked_it(
    client, users, departments, rooms, day
):
    created = post_booking(
        client,
        users["rahul"],
        room_id="pulse",
        day=day,
        entry="09:00",
        exit_="10:00",
        department_id=departments["Finance"].id,
        conducted_by=users["joseph"].id,
    )
    response = client.post(
        f"/api/bookings/{created.json()['id']}/cancel",
        headers=headers_for(users["joseph"]),
    )
    assert response.status_code == 200


# =============================================================================
# T-13 — cancelling a running meeting releases the remaining time
# =============================================================================


def test_T13_cancelling_a_running_booking_releases_remaining_time(
    client, db, users, departments, rooms, monkeypatch
):
    """A 1 pm to 3 pm booking cancelled at 1:40 pm frees the room from then on.

    The clock is frozen at 13:40 local on a future working day so the test is
    deterministic at any hour. The booking is inserted directly because the API
    rightly refuses to create one that has already started.
    """
    day = working_day(20)
    frozen_local = datetime.combine(day, datetime.min.time()).replace(
        hour=13, minute=40, tzinfo=settings.timezone
    )
    monkeypatch.setattr(timeutil, "now_local", lambda: frozen_local)
    monkeypatch.setattr(
        timeutil, "now_utc", lambda: frozen_local.astimezone(timezone.utc)
    )

    running = Booking(
        id=uuid.uuid4(),
        room_id="power",
        entry_time=timeutil.local_datetime(day, 13 * 60),
        exit_time=timeutil.local_datetime(day, 15 * 60),
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        booked_by=users["rahul"].id,
        title=f"{TEST_TITLE_PREFIX} running meeting",
        status=BookingStatus.CONFIRMED,
    )
    db.add(running)
    db.commit()

    # While it is running, the room is taken.
    blocked = post_booking(
        client,
        users["neha"],
        room_id="power",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert blocked.status_code == 409

    cancelled = client.post(
        f"/api/bookings/{running.id}/cancel", headers=headers_for(users["rahul"])
    )
    assert cancelled.status_code == 200

    # The remaining time, 14:00 to 15:00, is now bookable by anyone.
    freed = post_booking(
        client,
        users["neha"],
        room_id="power",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert freed.status_code == 201, freed.text


# =============================================================================
# No-show — D-06
# =============================================================================


@pytest.fixture()
def started_booking(db, users, departments, rooms, monkeypatch):
    """A booking that started 20 minutes ago, with the clock frozen."""
    day = working_day(21)
    frozen_local = datetime.combine(day, datetime.min.time()).replace(
        hour=13, minute=20, tzinfo=settings.timezone
    )
    monkeypatch.setattr(timeutil, "now_local", lambda: frozen_local)
    monkeypatch.setattr(
        timeutil, "now_utc", lambda: frozen_local.astimezone(timezone.utc)
    )

    row = Booking(
        id=uuid.uuid4(),
        room_id="ignite",
        entry_time=timeutil.local_datetime(day, 13 * 60),
        exit_time=timeutil.local_datetime(day, 15 * 60),
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        booked_by=users["rahul"].id,
        title=f"{TEST_TITLE_PREFIX} no-show candidate",
        status=BookingStatus.CONFIRMED,
    )
    db.add(row)
    db.commit()
    return row, day, frozen_local


def test_no_show_before_fifteen_minutes_is_refused(
    client, db, users, departments, rooms, monkeypatch
):
    day = working_day(22)
    # Only 5 minutes past the start.
    frozen_local = datetime.combine(day, datetime.min.time()).replace(
        hour=13, minute=5, tzinfo=settings.timezone
    )
    monkeypatch.setattr(timeutil, "now_local", lambda: frozen_local)
    monkeypatch.setattr(
        timeutil, "now_utc", lambda: frozen_local.astimezone(timezone.utc)
    )

    row = Booking(
        id=uuid.uuid4(),
        room_id="spark",
        entry_time=timeutil.local_datetime(day, 13 * 60),
        exit_time=timeutil.local_datetime(day, 14 * 60),
        department_id=departments["Sales"].id,
        conducted_by=users["rahul"].id,
        booked_by=users["rahul"].id,
        title=f"{TEST_TITLE_PREFIX} too early",
        status=BookingStatus.CONFIRMED,
    )
    db.add(row)
    db.commit()

    response = client.post(
        f"/api/bookings/{row.id}/no-show", headers=headers_for(users["reception"])
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.NO_SHOW_TOO_EARLY.format(
        minutes=settings.no_show_release_minutes, when="1:15 pm"
    )


def test_reception_may_release_a_no_show_after_fifteen_minutes(
    client, db, users, started_booking
):
    row, day, _ = started_booking

    response = client.post(
        f"/api/bookings/{row.id}/no-show", headers=headers_for(users["reception"])
    )
    assert response.status_code == 200
    assert response.json()["status"] == "NO_SHOW"

    # Audit-logged...
    assert "NO_SHOW" in [a.action for a in audit_for(db, row.id)]
    # ...and deliberately not notified.
    assert logs_for(db, row.id) == []


def test_only_reception_or_admin_may_mark_a_no_show(client, users, started_booking):
    row, _, _ = started_booking
    response = client.post(
        f"/api/bookings/{row.id}/no-show", headers=headers_for(users["rahul"])
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.CANNOT_MARK_NO_SHOW


def test_no_show_window_is_immediately_rebookable_by_anyone(
    client, users, departments, rooms, started_booking
):
    """A released slot is bookable by anyone, including the original host."""
    row, day, _ = started_booking

    client.post(
        f"/api/bookings/{row.id}/no-show", headers=headers_for(users["admin"])
    )

    taken = post_booking(
        client,
        users["neha"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert taken.status_code == 201, taken.text


def test_restoring_a_no_show_is_refused_when_the_window_was_taken(
    client, users, departments, rooms, started_booking
):
    """The exclusion constraint adjudicates; nothing is pre-checked and trusted."""
    row, day, _ = started_booking

    client.post(
        f"/api/bookings/{row.id}/no-show", headers=headers_for(users["admin"])
    )
    stolen = post_booking(
        client,
        users["neha"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Marketing"].id,
        conducted_by=users["neha"].id,
    )
    assert stolen.status_code == 201

    restored = client.post(
        f"/api/bookings/{row.id}/restore", headers=headers_for(users["admin"])
    )
    assert restored.status_code == 409
    assert "Ignite" in restored.json()["detail"]


def test_restoring_a_no_show_succeeds_when_the_window_is_still_free(
    client, db, users, started_booking
):
    row, _, _ = started_booking

    client.post(
        f"/api/bookings/{row.id}/no-show", headers=headers_for(users["admin"])
    )
    restored = client.post(
        f"/api/bookings/{row.id}/restore", headers=headers_for(users["admin"])
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "CONFIRMED"
    assert "RESTORED" in [a.action for a in audit_for(db, row.id)]


# =============================================================================
# Editing the details
# =============================================================================


def test_editing_details_fires_changed(client, db, users, departments, booking):
    response = client.patch(
        f"/api/bookings/{booking['id']}",
        json={
            "title": f"{TEST_TITLE_PREFIX} renamed",
            "department_id": departments["Marketing"].id,
            "reception_note": "Projector and tea for 6",
        },
        headers=headers_for(users["rahul"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == f"{TEST_TITLE_PREFIX} renamed"
    assert body["department"] == "Marketing"
    assert body["reception_note"] == "Projector and tea for 6"

    changed = [
        r
        for r in logs_for(db, booking["id"])
        if r.event == NotificationEvent.CHANGED
    ]
    assert len(changed) == 5

    assert "UPDATED" in [a.action for a in audit_for(db, booking["id"])]


def test_room_date_and_time_can_never_be_edited(client, users, booking, day):
    for field, value in (
        ("room_id", "pulse"),
        ("date", (day + timedelta(days=1)).isoformat()),
        ("entry", "11:00"),
        ("exit", "12:00"),
    ):
        response = client.patch(
            f"/api/bookings/{booking['id']}",
            json={field: value},
            headers=headers_for(users["rahul"]),
        )
        assert response.status_code == 400, field
        assert response.json()["detail"] == messages.IMMUTABLE_FIELDS


def test_attendee_churn_notifies_each_group_correctly(
    client, db, users, booking
):
    """Added get BOOKED, removed get CANCELLED, everyone else CHANGED."""
    before_ids = {a["id"] for a in booking["attendees"]}
    assert before_ids == {users["priya"].id, users["aditi"].id}

    response = client.patch(
        f"/api/bookings/{booking['id']}",
        # Drop Aditi, keep Priya, add Imran.
        json={"attendee_ids": [users["priya"].id, users["imran"].id]},
        headers=headers_for(users["rahul"]),
    )
    assert response.status_code == 200, response.text
    assert {a["full_name"] for a in response.json()["attendees"]} == {
        "Priya Nair",
        "Imran Sheikh",
    }

    rows = logs_for(db, booking["id"])

    def events_for(email):
        return [r.event for r in rows if r.recipient == email]

    # Imran is new: told BOOKED, and not also told it changed.
    assert events_for("imran.sheikh@shreenm.com") == [NotificationEvent.BOOKED]
    # Aditi was dropped: told CANCELLED.
    assert NotificationEvent.CANCELLED in events_for("aditi.shah@shreenm.com")
    # Priya stays: told BOOKED at creation, then CHANGED.
    assert events_for("priya.nair@shreenm.com") == [
        NotificationEvent.BOOKED,
        NotificationEvent.CHANGED,
    ]


def test_an_attendee_cannot_edit(client, users, booking):
    response = client.patch(
        f"/api/bookings/{booking['id']}",
        json={"title": f"{TEST_TITLE_PREFIX} hijacked"},
        headers=headers_for(users["priya"]),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.CANNOT_EDIT.format(
        owner="Rahul Mehta"
    )


def test_a_cancelled_booking_cannot_be_edited(client, users, booking):
    client.post(
        f"/api/bookings/{booking['id']}/cancel",
        headers=headers_for(users["rahul"]),
    )
    response = client.patch(
        f"/api/bookings/{booking['id']}",
        json={"title": f"{TEST_TITLE_PREFIX} too late"},
        headers=headers_for(users["rahul"]),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == messages.CANNOT_CHANGE_CANCELLED


# =============================================================================
# Attendee responses
# =============================================================================


def test_an_attendee_may_accept_or_decline_their_own_place(
    client, db, users, booking
):
    response = client.post(
        f"/api/bookings/{booking['id']}/response",
        json={"response": "DECLINED"},
        headers=headers_for(users["priya"]),
    )
    assert response.status_code == 200

    priya = next(
        a for a in response.json()["attendees"] if a["full_name"] == "Priya Nair"
    )
    assert priya["response_status"] == "DECLINED"

    # Aditi is untouched — an attendee changes only their own row.
    aditi = next(
        a for a in response.json()["attendees"] if a["full_name"] == "Aditi Shah"
    )
    assert aditi["response_status"] == "PENDING"


def test_a_response_sends_no_notification(client, db, users, booking):
    before = len(logs_for(db, booking["id"]))

    client.post(
        f"/api/bookings/{booking['id']}/response",
        json={"response": "ACCEPTED"},
        headers=headers_for(users["priya"]),
    )

    assert len(logs_for(db, booking["id"])) == before


def test_a_non_attendee_cannot_respond(client, users, booking):
    response = client.post(
        f"/api/bookings/{booking['id']}/response",
        json={"response": "ACCEPTED"},
        headers=headers_for(users["imran"]),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.NOT_AN_ATTENDEE


def test_declining_does_not_cancel_the_meeting(client, db, users, booking, day):
    client.post(
        f"/api/bookings/{booking['id']}/response",
        json={"response": "DECLINED"},
        headers=headers_for(users["priya"]),
    )

    row = db.scalar(select(Booking).where(Booking.id == uuid.UUID(booking["id"])))
    assert row.status == BookingStatus.CONFIRMED

    availability = client.get(f"/api/availability?date={day.isoformat()}", headers=headers_for(users["priya"])).json()
    switch = next(r for r in availability["rooms"] if r["id"] == "switch")
    assert len(switch["bookings"]) == 1

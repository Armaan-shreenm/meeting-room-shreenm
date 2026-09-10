"""Phase 3 - cancel, edit, no-show, attendee response and notifications.

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
from app.database import SessionLocal
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
# T-09 - notifications on booking
# =============================================================================


def test_T09_two_attendees_produce_five_notifications(db, booking):
    """Two attendees, the conductor, reception and the Mumbai group.

    Rahul is both booker and conductor here, so he is one recipient, not two - 
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


# -----------------------------------------------------------------------------
# Delivery off the request thread
# -----------------------------------------------------------------------------
# The rest of the suite runs synchronously (see conftest). These three turn the
# production path back on, because that is the one the receptionist uses.


@pytest.fixture()
def async_delivery(monkeypatch):
    """Production behaviour: queue on commit, send on the background thread."""
    monkeypatch.setattr(settings, "notifications_async", True)


class _Recorder:
    """A transport that remembers, and can be told to fail."""

    name = "recorder"

    def __init__(self, explode: bool = False) -> None:
        self.sent: list[str] = []
        self.explode = explode
        self.seen = __import__("threading").Event()

    def send(self, message) -> None:
        if self.explode:
            self.seen.set()
            raise RuntimeError("the relay refused it")
        self.sent.append(message.recipient.email)
        self.seen.set()


def _settle(db, booking_id, want, timeout=10.0):
    """Wait for the delivery thread to finish writing back."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        db.expire_all()
        rows = logs_for(db, booking_id)
        done = [r for r in rows if r.status != NotificationStatus.QUEUED]
        if len(done) >= want:
            return rows
        time.sleep(0.05)
    return logs_for(db, booking_id)


def test_the_request_does_not_wait_for_the_mail_to_be_sent(
    client, db, users, departments, rooms, day, async_delivery
):
    """A slow transport must not slow the booking down.

    This is the ten-second spinner the receptionist used to sit through: three
    messages, three or four seconds each, all inside the POST.
    """
    import time

    class _Slow:
        name = "slow"

        def __init__(self):
            self.sent = 0

        def send(self, message):
            time.sleep(0.6)
            self.sent += 1

    slow = _Slow()
    notifications.set_transport(slow)
    try:
        started = time.monotonic()
        created = post_booking(
            client,
            users["rahul"],
            room_id="ignite",
            day=day,
            entry="14:00",
            exit_="15:00",
            department_id=departments["Finance"].id,
            conducted_by=users["rahul"].id,
        )
        elapsed = time.monotonic() - started
        assert created.status_code == 201, created.text

        # Three messages at 0.6s each is 1.8s of sending. The response must not
        # have carried any of it.
        assert elapsed < 1.0, f"the POST waited {elapsed:.2f}s for the transport"

        # Nothing is deferred any more, so every recipient is a real send.
        want = len(logs_for(db, created.json()["id"]))
        assert want >= 3

        rows = _settle(db, created.json()["id"], want=want)
        assert slow.sent == want
        assert all(
            r.status == NotificationStatus.SENT for r in rows
        ), [(r.recipient, r.status.value) for r in rows]
    finally:
        notifications.set_transport(notifications.StdoutTransport())


def test_a_message_that_fails_on_the_thread_is_recorded_not_lost(
    client, db, users, departments, rooms, day, async_delivery
):
    """The row has to end FAILED with a reason, exactly as it would inline."""
    boom = _Recorder(explode=True)
    notifications.set_transport(boom)
    try:
        created = post_booking(
            client,
            users["rahul"],
            room_id="ignite",
            day=day,
            entry="14:00",
            exit_="15:00",
            department_id=departments["Finance"].id,
            conducted_by=users["rahul"].id,
        )
        assert created.status_code == 201, created.text

        rows = _settle(db, created.json()["id"], want=3)
        failed = [r for r in rows if r.status == NotificationStatus.FAILED]
        assert failed, [(r.recipient, r.status) for r in rows]
        assert "the relay refused it" in failed[0].error
    finally:
        notifications.set_transport(notifications.StdoutTransport())


def test_a_booking_that_never_commits_announces_nothing(
    client, db, users, departments, rooms, day, async_delivery
):
    """Queuing happens on commit, so a rolled-back transaction sends no mail.

    This is the rule that makes deferring delivery safe at all. Queuing at send
    time instead would both race the commit and promise a room that the database
    went on to refuse.
    """
    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text
    _settle(db, created.json()["id"], want=3)

    # Only now, so the recorder sees nothing but the rolled-back attempt.
    recorder = _Recorder()
    notifications.set_transport(recorder)
    try:
        session = SessionLocal()
        try:
            booking = session.scalar(
                select(Booking).where(Booking.id == uuid.UUID(created.json()["id"]))
            )
            notifications.notify(session, booking, NotificationEvent.BOOKED)
            session.rollback()
        finally:
            session.close()

        assert not recorder.seen.wait(0.5), "a rolled-back booking sent mail"
        assert recorder.sent == []
    finally:
        notifications.set_transport(notifications.StdoutTransport())


# -----------------------------------------------------------------------------
# The branch list
# -----------------------------------------------------------------------------
# Reception books on behalf of people who are not on the booking: nobody is
# named as an attendee and the host is the receptionist. The branch list is the
# only audience that would otherwise never hear that a room had gone.


@pytest.fixture()
def branch_group_at(monkeypatch):
    """Point MUMBAI_GROUP_EMAIL somewhere and hand back the address."""

    def _set(email: str) -> str:
        monkeypatch.setattr(settings, "mumbai_group_email", email)
        return email

    return _set


def _html_for(booking_row, db, kind, event=NotificationEvent.BOOKED):
    """Render one booking's mail as the given kind of recipient would see it."""
    booking = db.scalar(
        select(Booking).where(Booking.id == uuid.UUID(str(booking_row["id"])))
    )
    recipient = notifications.Recipient(
        email="whoever@shreenm.com", name="Who Ever", kind=kind
    )
    return notifications.render_html(booking, event, recipient)


def test_the_branch_list_gets_an_update_not_a_confirmation(
    client, db, users, departments, rooms, day
):
    """Nobody on the branch list asked for the room.

    Telling forty people "Your Meeting Room is Booked!" reads as a mistake the
    first time and as noise every time after.
    """
    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text
    body = created.json()

    to_group = _html_for(body, db, notifications.BRANCH_GROUP)
    assert notifications.GROUP_TITLE in to_group
    assert notifications.GROUP_BADGE in to_group
    assert "booked by another person" in to_group
    assert "Your Meeting Room is Booked" not in to_group
    # A distribution list is not a person to say hello to.
    assert "Hi " not in to_group

    to_booker = _html_for(body, db, notifications.BOOKER)
    assert "Your Meeting Room is Booked" in to_booker
    assert notifications.GROUP_TITLE not in to_booker
    assert ">MEET<" in to_booker

    # The front desk did not ask for the room either, so it reads the notice.
    to_reception = _html_for(body, db, notifications.RECEPTION)
    assert notifications.GROUP_TITLE in to_reception
    assert "Your Meeting Room is Booked" not in to_reception


def test_a_cancellation_reaches_the_branch_list_as_an_update(
    client, db, users, departments, rooms, day
):
    """The booker is told theirs is cancelled; the branch is told the room is free."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text
    body = created.json()

    to_group = _html_for(
        body, db, notifications.BRANCH_GROUP, NotificationEvent.CANCELLED
    )
    assert notifications.GROUP_TITLE in to_group
    assert "free again" in to_group

    to_booker = _html_for(body, db, notifications.BOOKER, NotificationEvent.CANCELLED)
    assert "Your Booking Was Cancelled" in to_booker


def test_both_audiences_are_invited_to_book_a_room(
    client, db, users, departments, rooms, day
):
    """The footer is the same on both: whoever read it may want a room too."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text
    body = created.json()

    for kind in (
        notifications.BOOKER,
        notifications.RECEPTION,
        notifications.BRANCH_GROUP,
    ):
        html = _html_for(body, db, kind)
        assert "Need another meeting room?" in html, kind
        assert "Book a Room" in html, kind


def test_the_branch_list_hears_about_every_booking(
    client, db, users, departments, rooms, day, branch_group_at
):
    where = branch_group_at("bookings.mumbai@shreenm.com")

    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text

    rows = {r.recipient: r for r in logs_for(db, created.json()["id"])}
    assert where in rows

    # Immediately. A room going in ten minutes is no use to anybody tomorrow
    # morning, which is why the daily digest was dropped.
    assert rows[where].status == NotificationStatus.SENT
    assert rows[where].sent_at is not None


def test_the_branch_list_does_not_double_up_on_somebody_already_told(
    client, db, users, departments, rooms, day, branch_group_at
):
    """Point it at the booker and they get one message, not two.

    Worth pinning: an earlier version had two separate entries for the branch -
    one immediate, one deferred - and pointing both at the same address made the
    deferred one win the deduplication, so the message was logged and never
    sent. One entry cannot lose that argument with itself.
    """
    branch_group_at(users["rahul"].email)

    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text

    rows = logs_for(db, created.json()["id"])
    addressed = [r.recipient for r in rows]
    assert addressed.count(users["rahul"].email) == 1

    # And the one copy is actually sent, not quietly parked.
    mine = next(r for r in rows if r.recipient == users["rahul"].email)
    assert mine.status == NotificationStatus.SENT


def test_nobody_is_mailed_by_default(client, db, users, departments, rooms, day):
    """The branch list ships empty, and that is the point.

    A real-looking default would be a trap: deleting the variable from a
    dashboard reads as "stop mailing the branch" and would instead start mailing
    whatever the default named - a mailbox nobody reads, or one that bounces
    every booking. Nobody is written to unless somebody says who.
    """
    from app.config import Settings

    assert Settings.model_fields["mumbai_group_email"].default == ""


def test_the_branch_list_can_be_several_addresses(
    client, db, users, departments, rooms, day, branch_group_at
):
    """One distribution list eventually; a few named people until it exists."""
    branch_group_at("first@shreenm.com, second@shreenm.com")

    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text

    rows = {r.recipient: r for r in logs_for(db, created.json()["id"])}
    assert "first@shreenm.com" in rows
    assert "second@shreenm.com" in rows
    assert rows["first@shreenm.com"].status == NotificationStatus.SENT
    assert rows["second@shreenm.com"].status == NotificationStatus.SENT


def test_whitespace_around_a_branch_address_is_forgiven(
    client, db, users, departments, rooms, day, branch_group_at
):
    """These are typed into a dashboard by hand.

    A value pasted with a stray space has already broken this deployment twice -
    once on SMTP_HOST, once on GOOGLE_REDIRECT_URI - so the addresses are
    stripped rather than trusted.
    """
    branch_group_at("  spaced@shreenm.com  ,	second@shreenm.com ")

    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text

    addressed = [r.recipient for r in logs_for(db, created.json()["id"])]
    assert "spaced@shreenm.com" in addressed
    assert "second@shreenm.com" in addressed
    assert not any(a != a.strip() for a in addressed)


def test_no_branch_list_means_no_extra_recipient(
    client, db, users, departments, rooms, day, branch_group_at
):
    """A deployment with no branch list must not address the empty string."""
    branch_group_at("")

    created = post_booking(
        client,
        users["rahul"],
        room_id="ignite",
        day=day,
        entry="14:00",
        exit_="15:00",
        department_id=departments["Finance"].id,
        conducted_by=users["rahul"].id,
    )
    assert created.status_code == 201, created.text

    addressed = [r.recipient for r in logs_for(db, created.json()["id"])]
    assert "" not in addressed
    assert len(addressed) == len(set(addressed))


def test_the_branch_group_is_told_immediately_like_everybody_else(db, booking):
    """It used to be held back for a daily 8 am digest. It is not any more.

    Reception books for people who are not on the booking, so the branch list is
    the only audience that would otherwise never hear - and hearing tomorrow
    morning about a room already used is no use to anybody. Nothing is deferred:
    every row is sent.
    """
    rows = {r.recipient: r for r in logs_for(db, booking["id"])}

    branch = rows[settings.mumbai_group_email]
    assert branch.status == NotificationStatus.SENT
    assert branch.sent_at is not None

    for email, row in rows.items():
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
# T-10 - cancelling frees the window and notifies
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

    # Never deleted - kept for the audit record.
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
# T-11 / T-12 - who may cancel
# =============================================================================


@pytest.mark.parametrize("handle", ["reception", "admin", "priya", "imran", "neha"])
def test_nobody_but_the_booker_may_cancel(client, users, booking, handle):
    """Privilege levels are gone. Reception and admin have no special rights."""
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel", headers=headers_for(users[handle])
    )
    assert response.status_code == 403, f"{handle}: {response.text}"
    assert response.json()["detail"] == messages.CANNOT_CANCEL.format(
        owner="Rahul Mehta"
    )


def test_T12_an_attendee_cannot_cancel(client, users, booking):
    """An attendee may only decline their own place."""
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


def test_the_conductor_cannot_cancel_unless_they_booked_it(
    client, users, departments, rooms, day
):
    """Running the meeting is not the same as having booked the room."""
    created = post_booking(
        client,
        users["rahul"],
        room_id="pulse",
        day=day,
        entry="17:00",
        exit_="18:00",
        department_id=departments["Finance"].id,
        conducted_by=users["joseph"].id,
    )
    response = client.post(
        f"/api/bookings/{created.json()['id']}/cancel",
        headers=headers_for(users["joseph"]),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == messages.CANNOT_CANCEL.format(
        owner="Rahul Mehta"
    )


# =============================================================================
# T-13 - cancelling a running meeting releases the remaining time
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
# No-show - removed with the privilege levels
# =============================================================================
# D-06 released an unused room after fifteen minutes, but only reception or an
# administrator could do it. Privilege levels no longer exist, so the endpoints
# and their tests went with them. BookingStatus.NO_SHOW remains in the schema.

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

    # Aditi is untouched - an attendee changes only their own row.
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

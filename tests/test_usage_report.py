"""The weekly usage report: its period, its two numbers, and how it is sent.

No test here sends mail. Every send goes to a recording transport, and the
suite-wide fixture in conftest pins the configured one to stdout besides.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import time as timeutil
from app.models import AuditLog, Booking, BookingStatus, User
from app.services import usage_report
from scripts import usage_report as usage_report_script

# A week long gone, so nothing real ever lands in it.
START = date(2020, 1, 6)  # Monday
END = date(2020, 1, 11)  # Saturday


def at(day: date, hour: int = 12) -> datetime:
    """An instant on a branch-local day."""
    return timeutil.local_datetime(day, hour * 60)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.sent = []

    def deliver(self, mail) -> None:
        self.sent.append(mail)


@pytest.fixture()
def recorder(monkeypatch) -> Recorder:
    transport = Recorder()
    monkeypatch.setattr(usage_report, "select_transport", lambda: transport)
    return transport


@pytest.fixture()
def restore_logins(db, users):
    """Put every directory member's last_login_at back as it was."""
    before = {u.id: u.last_login_at for u in db.scalars(select(User)).all()}
    yield
    db.rollback()
    for user in db.scalars(select(User)).all():
        user.last_login_at = before.get(user.id)
    db.commit()


def add_booking(db, users, departments, rooms, *, booker, created, status=BookingStatus.CONFIRMED, slot=0):
    entry = at(START, 10) + timedelta(hours=slot)
    booking = Booking(
        id=uuid.uuid4(),
        room_id=next(iter(rooms)),
        entry_time=entry,
        exit_time=entry + timedelta(minutes=30),
        department_id=next(iter(departments.values())).id,
        conducted_by=booker.id,
        booked_by=booker.id,
        title="pytest usage",
        status=status,
        created_at=created,
    )
    db.add(booking)
    db.commit()
    return booking


# ------------------------------------------------------------------- period


@pytest.mark.parametrize(
    "today, expected",
    [
        # Sunday: the working week that ended yesterday.
        (date(2026, 9, 27), (date(2026, 9, 21), date(2026, 9, 26))),
        # Any other day: the last complete week, never a half-finished one.
        (date(2026, 9, 26), (date(2026, 9, 14), date(2026, 9, 19))),
        (date(2026, 9, 28), (date(2026, 9, 21), date(2026, 9, 26))),
        (date(2026, 10, 1), (date(2026, 9, 21), date(2026, 9, 26))),
    ],
)
def test_default_period_is_the_last_complete_working_week(today, expected):
    assert usage_report.default_period(today) == expected


def test_period_bounds_are_branch_local_midnights():
    since, until = usage_report._bounds(START, END)
    assert since == datetime(2020, 1, 5, 18, 30, tzinfo=timezone.utc)
    assert until == datetime(2020, 1, 11, 18, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------------ numbers


def test_people_signed_in_counts_each_person_once(db, users, departments, rooms, restore_logins):
    priya, rahul, sana, aditi = users["priya"], users["rahul"], users["sana"], users["aditi"]

    priya.last_login_at = at(START)          # signed in during the week
    rahul.last_login_at = at(END + timedelta(days=2))  # signed in again later...
    sana.last_login_at = at(START - timedelta(days=3))  # only before the week
    db.commit()

    # ...but Rahul booked during the week, which needed a session.
    add_booking(db, users, departments, rooms, booker=rahul, created=at(START + timedelta(days=1)))
    # Priya booking too must not count her twice.
    add_booking(db, users, departments, rooms, booker=priya, created=at(START), slot=1)
    # Aditi only cancelled something - an audited action, so a session.
    db.add(AuditLog(entity="booking", entity_id="x", action="CANCELLED", actor_id=aditi.id,
                    created_at=at(END)))
    db.commit()

    assert usage_report.count_people_signed_in(db, START, END) == 3


def test_the_period_edges_are_local_days(db, users, restore_logins):
    priya, rahul = users["priya"], users["rahul"]
    # 23:59 IST on the last day is inside; 00:00 IST the next day is not.
    priya.last_login_at = timeutil.local_datetime(END, 24 * 60 - 1)
    rahul.last_login_at = timeutil.local_datetime(END + timedelta(days=1), 0)
    db.commit()

    assert usage_report.count_people_signed_in(db, START, END) == 1


def test_with_sign_in_off_only_real_sign_ins_count(db, users, departments, rooms, monkeypatch, restore_logins):
    monkeypatch.setattr(settings, "sign_in_required", False)
    # Without sessions, booked_by is whoever the form named - not a sign-in.
    add_booking(db, users, departments, rooms, booker=users["rahul"], created=at(START))

    assert usage_report.count_people_signed_in(db, START, END) == 0


def test_meetings_scheduled_counts_bookings_made_in_the_period(db, users, departments, rooms):
    priya = users["priya"]
    add_booking(db, users, departments, rooms, booker=priya, created=at(START), slot=0)
    add_booking(db, users, departments, rooms, booker=priya, created=at(END), slot=1)
    # Cancelled afterwards: never became a meeting.
    add_booking(db, users, departments, rooms, booker=priya, created=at(START),
                status=BookingStatus.CANCELLED, slot=2)
    # Made outside the period.
    add_booking(db, users, departments, rooms, booker=priya,
                created=at(END + timedelta(days=1)), slot=3)

    assert usage_report.count_meetings_scheduled(db, START, END) == 2


def test_a_backwards_period_is_refused(db):
    with pytest.raises(ValueError):
        usage_report.build_report(db, END, START)


# -------------------------------------------------------------------- email


REPORT = usage_report.UsageReport(start=START, end=END, people_signed_in=12, meetings_scheduled=34)


def test_the_email_carries_both_numbers_and_the_period():
    html = usage_report.render_html(REPORT, None)
    text = usage_report.render_text(REPORT, None)

    for body in (html, text):
        assert "12" in body and "34" in body
        assert "Mon 06 Jan to Sat 11 Jan 2020" in body
    assert "People who signed in" in html and "Meetings scheduled" in html
    assert f"cid:{usage_report.LOGO_CID}" in html
    assert "Preview" not in html


def test_a_preview_says_so_in_the_subject_and_the_body(recorder):
    usage_report.send_report(REPORT, ["someone@shreenm.com"], preview_note="Preview from a test database.")

    (mail,) = recorder.sent
    assert mail["Subject"].startswith("[Preview] NM Meet weekly usage")
    assert "Preview from a test database." in mail.get_body(("html",)).get_content()


def test_one_message_per_recipient(recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_recipients", " a@shreenm.com, b@shreenm.com ")

    sent_to = usage_report.send_report(REPORT)

    assert sent_to == ["a@shreenm.com", "b@shreenm.com"]
    assert [m["To"] for m in recorder.sent] == ["a@shreenm.com", "b@shreenm.com"]
    assert recorder.sent[0]["From"].endswith(f"<{settings.smtp_from_email}>")


def test_nobody_configured_means_nothing_is_sent(recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_recipients", "")

    with pytest.raises(usage_report.ReportNotConfigured):
        usage_report.send_report(REPORT)
    assert recorder.sent == []


# ----------------------------------------------------------------- endpoint


URL = "/api/reports/weekly-usage"


def test_the_endpoint_does_not_exist_without_a_token(client, recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_token", "")
    assert client.post(URL, headers={"X-Report-Token": "anything"}).status_code == 404
    assert recorder.sent == []


def test_a_wrong_token_is_refused(client, recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_token", "right")
    assert client.post(URL, headers={"X-Report-Token": "wrong"}).status_code == 403
    assert client.post(URL).status_code == 403
    assert recorder.sent == []


def test_the_endpoint_sends_to_the_configured_recipients_only(client, recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_token", "right")
    monkeypatch.setattr(settings, "usage_report_recipients", "boss@shreenm.com")

    response = client.post(
        URL,
        headers={"X-Report-Token": "right"},
        json={"start": START.isoformat(), "end": END.isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["start"] == START.isoformat() and body["end"] == END.isoformat()
    assert body["sent_to"] == ["boss@shreenm.com"]
    assert [m["To"] for m in recorder.sent] == ["boss@shreenm.com"]


def test_the_endpoint_refuses_half_a_period_and_long_ones(client, recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_token", "right")
    monkeypatch.setattr(settings, "usage_report_recipients", "boss@shreenm.com")
    headers = {"X-Report-Token": "right"}

    assert client.post(URL, headers=headers, json={"start": START.isoformat()}).status_code == 400
    assert client.post(URL, headers=headers, json={"start": "2020-01-01", "end": "2020-03-01"}).status_code == 400
    assert recorder.sent == []


def test_the_endpoint_says_when_nobody_is_configured(client, recorder, monkeypatch):
    monkeypatch.setattr(settings, "usage_report_token", "right")
    monkeypatch.setattr(settings, "usage_report_recipients", "")

    assert client.post(URL, headers={"X-Report-Token": "right"}).status_code == 503


# ------------------------------------------------------------------- script


def test_the_script_sends_nothing_without_send(recorder, tmp_path):
    code = usage_report_script.main(
        ["--from", START.isoformat(), "--to", END.isoformat(),
         "--preview-file", str(tmp_path / "p.html")]
    )
    assert code == 0
    assert recorder.sent == []
    assert "data:image/png;base64," in (tmp_path / "p.html").read_text(encoding="utf-8")


def test_the_script_sends_only_to_an_explicit_recipient(recorder, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "usage_report_recipients", "boss@shreenm.com")
    code = usage_report_script.main(
        ["--send", "--recipient", "me@shreenm.com", "--preview-file", str(tmp_path / "p.html")]
    )
    assert code == 0
    assert [m["To"] for m in recorder.sent] == ["me@shreenm.com"]


def test_the_script_will_not_send_outside_the_company(recorder, tmp_path):
    code = usage_report_script.main(
        ["--send", "--recipient", "someone@gmail.com", "--preview-file", str(tmp_path / "p.html")]
    )
    assert code == 2
    assert recorder.sent == []

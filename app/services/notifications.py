"""Notifications - spec section 8.

Notifications are **always sent**. There is no toggle and no opt-out, so this
module has no "if enabled" branch around whether a recipient is told; the only
switch is which transport carries the message.

Five recipient groups, per the section 8 table:

* attendees, everyone added in the attendees field
* conducting, the person running the meeting
* reception, the Mumbai front desk mailbox
* Mumbai group, the branch-wide distribution list
* booker, whoever filled in the form

All five are told immediately. The Mumbai group used to be held back for a daily
8 am digest (decision D-01), which turned out to be exactly wrong for how the
system is actually used: reception books on everybody's behalf, so nobody is
named as an attendee and the host is the receptionist, and the branch is the
only audience that would otherwise never hear. A digest the next morning
announces rooms that have already been used. The digest is gone.

Every attempt writes a ``notification_log`` row ``QUEUED`` **before** the send,
then moves it to ``SENT`` or ``FAILED``. A crash mid-send leaves evidence rather
than silence.

The send itself happens on a background thread once the transaction commits -
see "delivery" below. The request returns as soon as the booking is safe, and
the row is moved to SENT or FAILED a moment later.

The transport in this phase renders the full message to stdout. Phase 7 puts SMTP
behind the same interface.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from html import escape
from typing import Protocol

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import settings
from app.core import time as timeutil
from app.models import (
    Booking,
    NotificationEvent,
    NotificationLog,
    NotificationStatus,
    User,
)

logger = logging.getLogger(__name__)

# Recipient kinds, matching the section 8 table.
ATTENDEE = "attendee"
CONDUCTOR = "conductor"
RECEPTION = "reception"
BRANCH_GROUP = "branch_group"
BOOKER = "booker"

_EVENT_HEADLINE = {
    NotificationEvent.BOOKED: "Room booked",
    NotificationEvent.CHANGED: "Booking changed",
    NotificationEvent.CANCELLED: "Booking cancelled",
}

# What the HTML message says in large type, and what colour it says it in.
_EVENT_TITLE = {
    NotificationEvent.BOOKED: "Your Meeting Room is Booked!",
    NotificationEvent.CHANGED: "Your Booking Has Changed",
    NotificationEvent.CANCELLED: "Your Booking Was Cancelled",
}
_EVENT_LEAD = {
    NotificationEvent.BOOKED: "Your meeting room booking has been confirmed.",
    NotificationEvent.CHANGED: "The details of your meeting room booking have changed.",
    NotificationEvent.CANCELLED: "This meeting room booking has been cancelled.",
}

# The branch list is not the booker and never was - nobody on it asked for the
# room. Telling forty people "Your Meeting Room is Booked!" reads as a mistake
# the first time and as noise every time after, so the same facts go out under
# a heading that says what this actually is: somebody else booked a room.
GROUP_TITLE = "Meeting Booking Update"
GROUP_BADGE = "UPDATE"
_GROUP_LEAD = {
    NotificationEvent.BOOKED: (
        "This meeting room has been booked by another person. "
        "Here are the details:"
    ),
    NotificationEvent.CHANGED: (
        "The details of this meeting room booking have changed. "
        "Here are the new details:"
    ),
    NotificationEvent.CANCELLED: (
        "This meeting room booking has been cancelled. "
        "The room is free again."
    ),
}

# The palette is index.html's, so a message and the app it came from look like
# one product. Cancellations borrow the grid's "unavailable" red instead.
BRAND = "#F5A300"
BRAND_DARK = "#DB9100"
CANCELLED = "#D6452F"
INK = "#1B1A17"
INK_2 = "#55524B"
MUTED = "#8C8880"
PAGE = "#FAF8F5"
LINE = "#E9E4DC"
SUNK = "#F7F5F1"

# Referenced from the HTML as <img src="cid:...">, attached by the transport.
LOGO_CID = "nm-meet-logo"


@dataclass(frozen=True)
class Recipient:
    """One addressee of one event."""

    email: str
    name: str
    kind: str


@dataclass(frozen=True)
class RenderedMessage:
    """A message ready to hand to a transport."""

    recipient: Recipient
    event: NotificationEvent
    subject: str
    body: str
    # The same message as HTML. Sent alongside the plain text, never instead of
    # it: a mail client that shows no HTML still gets a complete message, and
    # the text part is what the notification log is really about.
    html: str = ""


class Transport(Protocol):
    """How a rendered message leaves the building."""

    name: str

    def send(self, message: RenderedMessage) -> None:
        """Deliver, or raise. Raising marks the log row FAILED."""


class StdoutTransport:
    """Phase 3 transport: log the full rendered message to stdout.

    The Render filesystem is ephemeral, so stdout is the only place anything is
    written. Phase 7 adds SMTP behind this same interface.
    """

    name = "stdout"

    def send(self, message: RenderedMessage) -> None:
        logger.info(
            "NOTIFY %s -> %s (%s)\nSubject: %s\n%s\n%s",
            message.event.value,
            message.recipient.email,
            message.recipient.kind,
            message.subject,
            message.body,
            "-" * 64,
        )


_transport: Transport = StdoutTransport()


def get_transport() -> Transport:
    return _transport


def set_transport(transport: Transport) -> None:
    """Swap the transport. Phase 7 uses this to install SMTP."""
    global _transport
    _transport = transport


# --------------------------------------------------------------- delivery

# SMTP is slow. Gmail takes three or four seconds per message, a booking
# produces several, and all of it used to happen inside the POST - so the
# receptionist watched a spinner for ten seconds after pressing "Confirm
# booking". Delivery now happens on one background thread instead.
#
# Two rules make that safe rather than merely faster:
#
# 1. Nothing is queued until the transaction **commits**. The work is parked on
#    the Session and released by an ``after_commit`` listener, so a booking that
#    rolls back announces nothing. Queuing at send time would have raced the
#    commit and looked up a row that did not exist yet.
# 2. The message is rendered on the request's thread, while the booking and its
#    room, department and attendees are still loaded. The thread receives plain
#    strings and never touches a detached ORM object.
#
# The log stays honest throughout: the row commits QUEUED and the thread moves
# it to SENT or FAILED a moment later. A process killed in between leaves a
# QUEUED row, which is the truth - that message never went.

_SESSION_PENDING = "nm_meet_pending_notifications"

_outbox: "queue.Queue[tuple[int, RenderedMessage]]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _deliver_one(row_id: int, message: RenderedMessage) -> None:
    """Send one message and record what happened, in its own session."""
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        row = session.get(NotificationLog, row_id)
        if row is None:
            logger.error("Notification row %s vanished before delivery", row_id)
            return

        try:
            get_transport().send(message)
        except Exception as exc:  # noqa: BLE001 - the reason is stored, not swallowed
            row.status = NotificationStatus.FAILED
            row.error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Notification to %s failed: %s", message.recipient.email, exc
            )
        else:
            row.status = NotificationStatus.SENT
            row.sent_at = timeutil.now_utc()

        session.commit()
    except Exception:  # noqa: BLE001 - one bad message must not kill the thread
        logger.exception("Delivering notification %s failed outright", row_id)
        session.rollback()
    finally:
        session.close()


def _run_outbox() -> None:
    while True:
        row_id, message = _outbox.get()
        try:
            _deliver_one(row_id, message)
        finally:
            _outbox.task_done()


def _ensure_worker() -> None:
    """Start the sender thread on first use, and only once."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(
            target=_run_outbox, name="nm-meet-notifier", daemon=True
        )
        _worker.start()
        logger.info("Notification delivery thread started")


def drain(timeout: float = 10.0) -> bool:
    """Wait for the outbox to empty. Called on shutdown; returns whether it did.

    A daemon thread dies with the process, so without this a redeploy could drop
    a message that was already committed as QUEUED.
    """
    if _outbox.unfinished_tasks == 0:
        return True

    logger.info("Waiting for %s notification(s) to go out", _outbox.unfinished_tasks)
    finished = threading.Event()

    def _wait() -> None:
        _outbox.join()
        finished.set()

    threading.Thread(target=_wait, daemon=True).start()
    if finished.wait(timeout):
        return True

    logger.warning(
        "Shut down with %s notification(s) still queued; they stay QUEUED in the log",
        _outbox.unfinished_tasks,
    )
    return False


@event.listens_for(Session, "after_commit")
def _release_pending(session: Session) -> None:
    """The booking is committed, so the messages about it may now go."""
    pending = session.info.pop(_SESSION_PENDING, None)
    if not pending:
        return

    _ensure_worker()
    for item in pending:
        _outbox.put(item)


@event.listens_for(Session, "after_rollback")
@event.listens_for(Session, "after_soft_rollback")
def _discard_pending(session: Session, *args: object) -> None:
    """Nothing happened, so nobody hears about it."""
    session.info.pop(_SESSION_PENDING, None)


# ------------------------------------------------------------- recipients


def recipients_for(booking: Booking) -> list[Recipient]:
    """The five groups from section 8, deduplicated by email address.

    The booker is very often also the conductor; they get one message, not two.
    Order is stable so the log reads predictably.
    """
    collected: list[Recipient] = []

    for attendee in booking.attendees:
        collected.append(
            Recipient(
                email=attendee.user.email,
                name=attendee.user.full_name,
                kind=ATTENDEE,
            )
        )

    collected.append(
        Recipient(
            email=booking.conductor.email,
            name=booking.conductor.full_name,
            kind=CONDUCTOR,
        )
    )
    collected.append(
        Recipient(
            email=booking.booker.email,
            name=booking.booker.full_name,
            kind=BOOKER,
        )
    )
    collected.append(
        Recipient(
            email=settings.reception_email,
            name=f"Reception, {settings.branch_name}",
            kind=RECEPTION,
        )
    )
    # The branch. Reception books for people who are not on the booking, so
    # without this nobody outside the front desk hears that a room has gone.
    # Usually one distribution list; several addresses while that list is still
    # a few named people.
    for address in settings.mumbai_group_emails:
        collected.append(
            Recipient(
                email=address,
                name=f"{settings.branch_name} branch group",
                kind=BRANCH_GROUP,
            )
        )

    unique: dict[str, Recipient] = {}
    for recipient in collected:
        key = recipient.email.lower()
        if key not in unique:
            unique[key] = recipient
    return list(unique.values())


# ---------------------------------------------------------------- rendering


def booking_link(booking: Booking) -> str:
    """The "view or cancel this booking" link section 8 requires."""
    return f"{settings.public_base_url.rstrip('/')}/?booking={booking.id}"


TEMPLATE = """<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:%(page)s;">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"
       style="background:%(page)s;padding:16px 10px;">
<tr><td align="center">

<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
       style="max-width:560px;width:100%%;background:#FFFFFF;border:1px solid %(line)s;
              border-radius:12px;overflow:hidden;">

  <tr><td style="padding:16px 20px;border-bottom:1px solid %(line)s;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="padding-right:10px;">
        <img src="cid:%(logo_cid)s" width="30" height="30" alt="Shree NM"
             style="display:block;border:0;"></td>
      <td style="font-family:%(font)s;font-size:17px;font-weight:bold;color:%(ink)s;
                 letter-spacing:-0.2px;white-space:nowrap;">Shree NM</td>
      <td style="padding-left:8px;">
        <span style="font-family:%(font)s;font-size:9px;font-weight:bold;color:#FFFFFF;
                     background:%(accent)s;padding:3px 6px;border-radius:3px;
                     letter-spacing:1.2px;">%(badge)s</span></td>
    </tr></table>
  </td></tr>

  <tr><td align="center" style="padding:20px 20px 0 20px;">
    <div style="font-family:%(font)s;font-size:19px;line-height:1.3;font-weight:bold;
                color:%(ink)s;">%(title)s</div>
    <div style="font-family:%(font)s;font-size:13px;color:%(muted)s;padding-top:7px;">
      %(intro)s</div>
  </td></tr>

  <tr><td style="padding:16px 20px 0 20px;">
    <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"
           style="border:1px solid %(line)s;border-radius:8px;overflow:hidden;">
      %(rows)s
    </table>
  </td></tr>

  <tr><td style="padding:16px 20px 20px 20px;">
    <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"
           style="background:%(sunk)s;border:1px solid %(line)s;border-radius:8px;">
      <tr><td align="center" style="padding:16px 18px;">
        <div style="font-family:%(font)s;font-size:14px;font-weight:bold;color:%(ink)s;
                    padding-bottom:12px;">Need another meeting room?</div>
        <a href="%(home)s"
           style="display:inline-block;background:%(accent)s;color:#FFFFFF;
                  font-family:%(font)s;font-size:14px;font-weight:bold;
                  text-decoration:none;padding:11px 26px;border-radius:6px;">
          Book a Room</a>
      </td></tr>
    </table>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def _greeting(recipient: Recipient) -> str:
    """Address a person by name; a shared mailbox is not a person."""
    if recipient.kind in (RECEPTION, BRANCH_GROUP):
        return "Hello,"
    first = recipient.name.split()[0] if recipient.name.strip() else ""
    return "Hi " + escape(first) + "," if first else "Hello,"


def _row(label: str, value: str, last: bool = False) -> str:
    """One line of the detail table.

    Tables and inline styles, not flexbox and a stylesheet: Outlook renders mail
    with Word's engine and Gmail drops most of what a browser would accept. This
    is the layout that survives both.

    The label column is narrow and the labels are short on purpose - on a phone
    the card is about 300px wide, and anything longer wraps onto three lines and
    pushes the details off the screen.
    """
    border = "" if last else "border-bottom:1px solid %s;" % LINE
    return (
        '<tr>'
        '<td style="%spadding:10px 14px;background:%s;width:34%%;'
        'font-family:Arial,Helvetica,sans-serif;font-size:13px;color:%s;">%s</td>'
        '<td style="%spadding:10px 14px;background:#FFFFFF;'
        'font-family:Arial,Helvetica,sans-serif;font-size:13px;color:%s;'
        'font-weight:bold;">%s</td>'
        '</tr>'
    ) % (border, SUNK, INK_2, escape(label), border, INK, escape(value))


def render_html(
    booking: Booking, event: NotificationEvent, recipient: Recipient
) -> str:
    """The same message, laid out.

    Five rows, not nine. A booking notice is read on a phone in a corridor, and
    everything that is not room, day, time, department or host is either already
    in the subject line or something the reader knew before they opened it.

    Two versions of it, decided by who is reading. The person who booked the
    room gets the confirmation. The branch list gets the same facts as an
    update, because nobody on that list asked for the room and addressing them
    as though they had is how a useful notice turns into ignored noise. Both
    end with the same invitation to book a room of their own.

    Everything is inline: no <style> block, no web font, no external image. The
    logo arrives as an attachment referenced by cid, so it shows even when the
    app itself is asleep - a hosted src would be a broken picture every time the
    free instance had spun down.
    """
    entry = timeutil.minutes_from_midnight(booking.entry_time)
    exit_ = timeutil.minutes_from_midnight(booking.exit_time)
    # "Tue, 08 Sep 2026" rather than "Tuesday 08 September 2026": the long form
    # wrapped to three lines in the value column on a phone.
    when = booking.booking_date.strftime("%a, %d %b %Y")

    accent = CANCELLED if event is NotificationEvent.CANCELLED else BRAND

    # The branch list is told about somebody else's booking; everybody else on
    # the message is on the booking itself.
    to_the_branch = recipient.kind == BRANCH_GROUP
    if to_the_branch:
        badge = GROUP_BADGE
        title = GROUP_TITLE
        # No greeting: a distribution list is not a person to say hello to.
        intro = escape(_GROUP_LEAD[event])
    else:
        badge = "MEET"
        title = _EVENT_TITLE[event]
        intro = _greeting(recipient) + " " + escape(_EVENT_LEAD[event])

    rows = [
        _row("Room", booking.room.name),
        _row("Date", when),
        _row(
            "Time",
            "%s to %s"
            % (timeutil.format_clock(entry), timeutil.format_clock(exit_)),
        ),
        _row("Department", booking.department.name),
        _row("Host", booking.conductor.full_name, last=True),
    ]

    home = escape(settings.public_base_url.rstrip("/") + "/", quote=True)
    font = "Arial,Helvetica,sans-serif"

    return TEMPLATE % {
        "page": PAGE,
        "line": LINE,
        "sunk": SUNK,
        "ink": INK,
        "ink2": INK_2,
        "muted": MUTED,
        "brand_dark": BRAND_DARK,
        "accent": accent,
        "font": font,
        "logo_cid": LOGO_CID,
        "badge": badge,
        "title": escape(title),
        "intro": intro,
        "rows": "".join(rows),
        "home": home,
    }


def render(booking: Booking, event: NotificationEvent, recipient: Recipient) -> RenderedMessage:
    """Everything section 8 says a message must contain."""
    entry = timeutil.minutes_from_midnight(booking.entry_time)
    exit_ = timeutil.minutes_from_midnight(booking.exit_time)
    when = booking.booking_date.strftime("%A %d %B %Y")

    headline = _EVENT_HEADLINE[event]
    subject = (
        f"{headline}: {booking.title} - {booking.room.name}, "
        f"{when}, {timeutil.format_clock(entry)}"
    )

    attendees = [a.user.full_name for a in booking.attendees]

    lines = [
        f"{headline} at {settings.branch_name}.",
        "",
        f"Room          {booking.room.name}",
        f"Date          {when}",
        f"Time          {timeutil.format_clock(entry)} to {timeutil.format_clock(exit_)}",
        f"Meeting       {booking.title}",
        f"Department    {booking.department.name}",
        f"Conducted by  {booking.conductor.full_name}",
        f"Attendees     {', '.join(attendees) if attendees else 'Nobody added'}",
    ]

    if booking.reception_note:
        lines.append(f"For reception {booking.reception_note}")

    lines += [
        f"Booked by     {booking.booker.full_name}",
        "",
        f"View or cancel this booking: {booking_link(booking)}",
    ]

    return RenderedMessage(
        recipient=recipient,
        event=event,
        subject=subject,
        body="\n".join(lines),
        html=render_html(booking, event, recipient),
    )


# ------------------------------------------------------------------ sending


def notify(
    db: Session, booking: Booking, event: NotificationEvent
) -> list[NotificationLog]:
    """Notify every recipient of one event. The caller owns the transaction.

    Returns the log rows written - one per recipient, always, whether the send
    succeeded or failed.
    """
    return notify_recipients(db, booking, event, recipients_for(booking))


def notify_recipients(
    db: Session,
    booking: Booking,
    event: NotificationEvent,
    recipients: list[Recipient],
) -> list[NotificationLog]:
    """Notify an explicit recipient list.

    Used when an edit sends different events to different people: someone newly
    added is told BOOKED, someone removed is told CANCELLED, and everybody else
    gets CHANGED.
    """
    transport = get_transport()
    rows: list[NotificationLog] = []

    for recipient in recipients:
        row = NotificationLog(
            booking_id=booking.id,
            recipient=recipient.email,
            event=event,
            status=NotificationStatus.QUEUED,
        )
        db.add(row)
        db.flush()  # the row exists before anything is attempted
        rows.append(row)

        # Rendered here, on this thread, while the booking's room, department,
        # conductor and attendees are all still loaded.
        message = render(booking, event, recipient)

        if settings.notifications_async:
            # Parked on the session; the after_commit listener releases it. The
            # row stays QUEUED until the sender thread has actually sent it.
            db.info.setdefault(_SESSION_PENDING, []).append((row.id, message))
            continue

        try:
            transport.send(message)
        except Exception as exc:  # noqa: BLE001 - the reason is stored, not swallowed
            row.status = NotificationStatus.FAILED
            row.error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Notification to %s failed: %s", recipient.email, exc, exc_info=True
            )
        else:
            row.status = NotificationStatus.SENT
            row.sent_at = timeutil.now_utc()

    db.flush()
    return rows


def notify_attendee_change(
    db: Session,
    booking: Booking,
    added: list[User],
    removed: list[User],
) -> list[NotificationLog]:
    """Attendee churn on an edit.

    Newly added attendees are told BOOKED, removed attendees CANCELLED, and
    everyone still on the booking gets CHANGED.
    """
    rows: list[NotificationLog] = []

    added_emails = {user.email.lower() for user in added}

    if added:
        rows += notify_recipients(
            db,
            booking,
            NotificationEvent.BOOKED,
            [
                Recipient(email=user.email, name=user.full_name, kind=ATTENDEE)
                for user in added
            ],
        )

    if removed:
        rows += notify_recipients(
            db,
            booking,
            NotificationEvent.CANCELLED,
            [
                Recipient(email=user.email, name=user.full_name, kind=ATTENDEE)
                for user in removed
            ],
        )

    # Everyone else on the booking hears that it changed. A person who was just
    # added is not also told it changed - they only just learned it exists.
    everyone_else = [
        recipient
        for recipient in recipients_for(booking)
        if recipient.email.lower() not in added_emails
    ]
    rows += notify_recipients(db, booking, NotificationEvent.CHANGED, everyone_else)

    return rows

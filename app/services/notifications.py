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
    if settings.mumbai_group_email:
        collected.append(
            Recipient(
                email=settings.mumbai_group_email,
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

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

D-01 settles the Mumbai group on a **daily 8 am summary** rather than a message
per booking. That does not remove it as a recipient: a row is still written for
every event, and it is left ``QUEUED`` for the daily job to pick up. Everyone
else is sent immediately. So the log always shows the full audience, and what
differs is when it goes out.

Every attempt writes a ``notification_log`` row ``QUEUED`` **before** the send,
then moves it to ``SENT`` or ``FAILED``. A crash mid-send leaves evidence rather
than silence.

The transport in this phase renders the full message to stdout. Phase 7 puts SMTP
behind the same interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

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
    """One addressee, and whether their copy goes now or in the daily summary."""

    email: str
    name: str
    kind: str
    deferred: bool = False


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
    # D-01: logged for every event, delivered in the daily 8 am summary.
    collected.append(
        Recipient(
            email=settings.mumbai_group_email,
            name=f"{settings.branch_name} branch group",
            kind=BRANCH_GROUP,
            deferred=True,
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
    succeeded, failed or was deferred to the daily summary.
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

        if recipient.deferred:
            # D-01: the branch group is collected into the daily 8 am summary.
            logger.info(
                "Deferred %s for %s to the daily summary",
                event.value,
                recipient.email,
            )
            continue

        message = render(booking, event, recipient)
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

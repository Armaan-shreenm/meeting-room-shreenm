"""Send the Mumbai branch group its daily 8 am summary - decision D-01.

Section 8 lists the branch distribution list as a recipient of every booking.
D-01 settles that it gets one summary a day instead, because a mail per booking
becomes noise. NM Meet honours both: a ``notification_log`` row is written for
the branch group on every event and left QUEUED, and this job is what delivers
them.

**This runs as a Render Cron Job, not inside the web service.** The free web
service sleeps after fifteen minutes, so an in-process scheduler cannot be
relied on to be awake at 08:00 IST. A cron job is a separate container that
Render starts on schedule, runs to completion and stops.

Idempotent: it only ever touches rows that are still QUEUED, so a retry after a
failure sends the ones that did not go, and a second run the same morning sends
nothing.

Usage:
    python -m scripts.daily_summary            # today's bookings, IST
    python -m scripts.daily_summary --date 2026-09-01
    python -m scripts.daily_summary --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core import time as timeutil
from app.core.logging import configure_logging
from app.database import SessionLocal
from app.models import (
    Booking,
    BookingStatus,
    NotificationLog,
    NotificationStatus,
)
from app.services import notifications
from app.services.notifications import Recipient, RenderedMessage
from app.services.transports import configure_transport

logger = logging.getLogger("scripts.daily_summary")


def queued_branch_rows(db: Session) -> list[NotificationLog]:
    """Every branch-group notification still waiting to go out."""
    return list(
        db.scalars(
            select(NotificationLog)
            .where(
                NotificationLog.recipient == settings.mumbai_group_email,
                NotificationLog.status == NotificationStatus.QUEUED,
            )
            .order_by(NotificationLog.created_at)
        ).all()
    )


def bookings_on(db: Session, day: date) -> list[Booking]:
    """Confirmed bookings for one local day, in start order."""
    return list(
        db.scalars(
            select(Booking)
            .where(
                Booking.booking_date == day,
                Booking.status == BookingStatus.CONFIRMED,
            )
            .options(
                selectinload(Booking.room),
                selectinload(Booking.department),
                selectinload(Booking.conductor),
                selectinload(Booking.booker),
            )
            .order_by(Booking.entry_time)
        ).all()
    )


def render_summary(day: date, bookings: list[Booking]) -> RenderedMessage:
    """One message listing the day's meetings, grouped by room."""
    when = day.strftime("%A %d %B %Y")
    recipient = Recipient(
        email=settings.mumbai_group_email,
        name=f"{settings.branch_name} branch group",
        kind=notifications.BRANCH_GROUP,
    )

    lines = [f"Meeting rooms at {settings.branch_name} for {when}.", ""]

    if not bookings:
        lines.append("Nothing is booked today.")
    else:
        by_room: dict[str, list[Booking]] = {}
        for booking in bookings:
            by_room.setdefault(booking.room.name, []).append(booking)

        for room_name in sorted(by_room):
            lines.append(room_name.upper())
            for booking in by_room[room_name]:
                entry = timeutil.format_clock(
                    timeutil.minutes_from_midnight(booking.entry_time)
                )
                exit_ = timeutil.format_clock(
                    timeutil.minutes_from_midnight(booking.exit_time)
                )
                lines.append(
                    f"  {entry} to {exit_}  {booking.title} "
                    f"({booking.department.name}, {booking.conductor.full_name})"
                )
            lines.append("")

        lines.append(f"{len(bookings)} meeting(s) in total.")

    lines += ["", f"Book a room: {settings.public_base_url.rstrip('/')}/"]

    return RenderedMessage(
        recipient=recipient,
        event=notifications.NotificationEvent.BOOKED,
        subject=f"NM Meet - {settings.branch_name} rooms for {when}",
        body="\n".join(lines),
    )


def run(day: date, dry_run: bool = False) -> int:
    """Send the summary and clear the queued branch rows. Returns rows cleared."""
    transport = configure_transport()
    logger.info("Daily summary for %s via %s transport", day.isoformat(), transport)

    with SessionLocal() as db:
        pending = queued_branch_rows(db)
        bookings = bookings_on(db, day)
        message = render_summary(day, bookings)

        logger.info(
            "%d booking(s) today, %d queued branch notification(s)",
            len(bookings),
            len(pending),
        )

        if dry_run:
            print(message.body)
            logger.info("Dry run - nothing sent, nothing marked.")
            return 0

        try:
            notifications.get_transport().send(message)
        except Exception as exc:  # noqa: BLE001 - recorded on every row, not swallowed
            logger.exception("Daily summary failed to send")
            for row in pending:
                row.status = NotificationStatus.FAILED
                row.error = f"{type(exc).__name__}: {exc}"
            db.commit()
            raise

        sent_at = timeutil.now_utc()
        for row in pending:
            row.status = NotificationStatus.SENT
            row.sent_at = sent_at
        db.commit()

        logger.info("Daily summary sent; %d row(s) marked SENT", len(pending))
        return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        help="Local date to summarise, YYYY-MM-DD. Defaults to today in the branch timezone.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the message, send nothing."
    )
    args = parser.parse_args()

    configure_logging()

    day = (
        date.fromisoformat(args.date) if args.date else timeutil.local_today()
    )

    try:
        run(day, dry_run=args.dry_run)
    except Exception:
        logger.exception("Daily summary job failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

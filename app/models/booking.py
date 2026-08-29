"""Bookings and their attendees - spec section 11.

``entry_time`` and ``exit_time`` are timestamptz in UTC. ``booking_date`` is the
calendar day the booking falls on in the branch timezone, which is the day the
grid groups by; it is stored rather than derived because 19:00 UTC is already
tomorrow in Mumbai.

Those two columns can drift, and no CHECK constraint can stop them: PostgreSQL
refuses ``AT TIME ZONE`` in a CHECK because it is STABLE, not IMMUTABLE. The
guarantee is made here instead, by a ``before_flush`` hook that derives the date
with :func:`app.core.time.booking_date_for` and refuses any Booking whose stored
date disagrees with its entry time.

The real defence against a double booking is not in this file. It is the
``no_double_booking`` EXCLUDE constraint added by the initial migration:

    EXCLUDE USING gist (
        room_id WITH =,
        tstzrange(entry_time, exit_time, '[)') WITH &&
    ) WHERE (status = 'CONFIRMED')

The half-open range is what makes a meeting ending at 15:00 and one starting at
15:00 back to back rather than a clash.

A booking's room, date and times are never editable. Changing when or where a
meeting happens is a cancellation and a fresh booking, so that the exclusion
constraint re-adjudicates it. Only the details - title, department, conducted_by,
attendees and the reception note - may be edited, and that raises CHANGED.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.config import settings
from app.core.time import booking_date_for
from app.database import Base
from app.models.enums import (
    ATTENDEE_RESPONSE_ENUM,
    BOOKING_STATUS_ENUM,
    AttendeeResponse,
    BookingStatus,
)

if TYPE_CHECKING:
    from app.models.department import Department
    from app.models.room import Room
    from app.models.user import User


class BookingDateMismatchError(ValueError):
    """Raised when ``booking_date`` contradicts ``entry_time``."""


class Booking(Base):
    """One reservation of one room for one window."""

    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    room_id: Mapped[str] = mapped_column(
        ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    # The branch-local calendar day the meeting starts on. Derived from
    # entry_time by app.core.time.booking_date_for and by nothing else; the
    # before_flush hook at the bottom of this module enforces that.
    booking_date: Mapped[date_type] = mapped_column(Date, nullable=False)

    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    exit_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    # Who is running the meeting. May differ from the person who filled the form.
    conducted_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    booked_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    title: Mapped[str] = mapped_column(
        String(120), nullable=False, default=settings.default_meeting_title
    )
    reception_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[BookingStatus] = mapped_column(
        PgEnum(BookingStatus, name=BOOKING_STATUS_ENUM, create_type=False),
        nullable=False,
        default=BookingStatus.CONFIRMED,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    room: Mapped["Room"] = relationship(back_populates="bookings")
    department: Mapped["Department"] = relationship(back_populates="bookings")
    conductor: Mapped["User"] = relationship(
        back_populates="bookings_conducted", foreign_keys=[conducted_by]
    )
    booker: Mapped["User"] = relationship(
        back_populates="bookings_made", foreign_keys=[booked_by]
    )
    attendees: Mapped[list["BookingAttendee"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("exit_time > entry_time", name="exit_after_entry"),
        # The grid queries one day of one room at a time.
        Index("ix_bookings_room_id_booking_date", "room_id", "booking_date"),
        Index("ix_bookings_booking_date_status", "booking_date", "status"),
        Index("ix_bookings_entry_time", "entry_time"),
    )

    def __repr__(self) -> str:
        return f"<Booking {self.id} {self.room_id} {self.entry_time}>"


class BookingAttendee(Base):
    """A colleague invited to a booking - spec field 7.

    Section 9: an attendee cannot cancel the meeting, only decline their place,
    which is what ``response_status`` records.
    """

    __tablename__ = "booking_attendees"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    response_status: Mapped[AttendeeResponse] = mapped_column(
        PgEnum(AttendeeResponse, name=ATTENDEE_RESPONSE_ENUM, create_type=False),
        nullable=False,
        default=AttendeeResponse.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    booking: Mapped["Booking"] = relationship(back_populates="attendees")
    user: Mapped["User"] = relationship(back_populates="attendances")

    __table_args__ = (
        # A person is invited to a meeting once.
        UniqueConstraint(
            "booking_id", "user_id", name="uq_booking_attendees_booking_id_user_id"
        ),
        Index("ix_booking_attendees_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<BookingAttendee booking={self.booking_id} user={self.user_id}>"


def _enforce_booking_date(booking: Booking) -> None:
    """Derive or verify ``booking_date`` against ``entry_time``.

    A booking with no date yet gets the derived one, so the column cannot be
    forgotten. A booking that already carries a date must agree with it, or the
    flush is refused - silently correcting it would hide the bug that produced
    the wrong value.
    """
    if booking.entry_time is None:
        return  # NOT NULL will reject it; that is not this hook's job.

    expected = booking_date_for(booking.entry_time)

    if booking.booking_date is None:
        booking.booking_date = expected
        return

    if booking.booking_date != expected:
        raise BookingDateMismatchError(
            f"booking_date {booking.booking_date.isoformat()} disagrees with "
            f"entry_time {booking.entry_time.isoformat()}, which is "
            f"{expected.isoformat()} in {settings.tz}. "
            "Set booking_date only via app.core.time.booking_date_for()."
        )


@event.listens_for(Session, "before_flush")
def _validate_bookings_before_flush(
    session: Session, flush_context: Any, instances: Any
) -> None:
    """Guard every Booking on its way to the database.

    Registered on the Session class, so it applies to every session in the
    process including the one Alembic and the scripts use.
    """
    for instance in list(session.new) + list(session.dirty):
        if isinstance(instance, Booking):
            _enforce_booking_date(instance)

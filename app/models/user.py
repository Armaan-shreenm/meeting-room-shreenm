"""The company directory - spec field 6, field 7 and section 9."""

from __future__ import annotations

from typing import TYPE_CHECKING

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.booking import Booking, BookingAttendee
    from app.models.department import Department


class User(Base):
    """A directory member.

    There are no roles. Everybody is an ordinary user: anyone may book a room,
    and only the person who booked one may cancel it.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # bcrypt hash. Nullable: a directory member who has never had a password set
    # simply cannot sign in, which is the safe default for a seeded row.
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    department: Mapped["Department | None"] = relationship(back_populates="users")
    bookings_made: Mapped[list["Booking"]] = relationship(
        back_populates="booker", foreign_keys="Booking.booked_by"
    )
    bookings_conducted: Mapped[list["Booking"]] = relationship(
        back_populates="conductor", foreign_keys="Booking.conducted_by"
    )
    attendances: Mapped[list["BookingAttendee"]] = relationship(
        back_populates="user"
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"

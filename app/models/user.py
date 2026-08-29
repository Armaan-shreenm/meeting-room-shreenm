"""The company directory — spec field 6, field 7 and section 9."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import USER_ROLE_ENUM, UserRole

if TYPE_CHECKING:
    from app.models.booking import Booking, BookingAttendee
    from app.models.department import Department


class User(Base):
    """A directory member.

    D-04 settled: only directory members may conduct a meeting, so this table is
    the whole permitted set for the "conducted by" and "attendees" fields.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    role: Mapped[UserRole] = mapped_column(
        PgEnum(UserRole, name=USER_ROLE_ENUM, create_type=False),
        nullable=False,
        default=UserRole.EMPLOYEE,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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

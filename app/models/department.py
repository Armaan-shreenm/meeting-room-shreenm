"""Departments — spec field 5."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.user import User


class Department(Base):
    """A branch department. Seeded, not user-created."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    users: Mapped[list["User"]] = relationship(back_populates="department")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="department")

    def __repr__(self) -> str:
        return f"<Department {self.name}>"

"""The five meeting rooms — spec section 1.

Rooms are seeded, never created by a user. The id is the lowercase room name and
is what the availability rules and the EXCLUDE constraint key on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.booking import Booking


class Room(Base):
    """Spark, Power, Pulse, Ignite or Switch.

    Spec field 1: rooms are shown by name only — no seat count and no equipment
    list, so neither is stored.
    """

    __tablename__ = "rooms"

    # 'spark' | 'power' | 'pulse' | 'ignite' | 'switch'
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The CSS custom property the frontend colours this room with.
    colour_var: Mapped[str] = mapped_column(String(32), nullable=False)
    # Advisory, not enforced: the form warns a small group off a large room.
    # Refusing a booking over a headcount nobody verifies would be worse.
    min_people: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="room")

    def __repr__(self) -> str:
        return f"<Room {self.id}>"

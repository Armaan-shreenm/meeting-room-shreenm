"""Declared holidays - spec field 2 and section 10.

A date in this table makes the office closed that day, alongside the weekly
closure from ``settings.closed_weekdays``.
"""

from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Holiday(Base):
    """One declared non-working day at the branch."""

    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date_type] = mapped_column(Date, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    def __repr__(self) -> str:
        return f"<Holiday {self.date} {self.name}>"

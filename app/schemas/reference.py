"""Reference data the booking form is built from - rooms, departments, directory."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RoomOut(BaseModel):
    """A meeting room. Name only - spec field 1 forbids seats and equipment."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Slug id: spark, power, pulse, ignite or switch.")
    name: str
    display_order: int
    colour_var: str = Field(
        description="CSS custom property the frontend colours this room with."
    )
    min_people: int = Field(
        description="Smallest party this room suits. Advisory, never enforced."
    )


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_order: int


class DirectoryUserOut(BaseModel):
    """An active directory member, for the host and attendee pickers."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    email: str
    department: str | None = Field(
        default=None, description="Department name, or null if unassigned."
    )

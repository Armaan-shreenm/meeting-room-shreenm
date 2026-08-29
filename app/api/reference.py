"""Reference data endpoints — rooms, departments and the directory.

These three fill the pickers the booking wizard is built from. They are read-only
in every phase: rooms and departments are seeded, and the directory is
admin-maintained.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Department, Room, User
from app.schemas.reference import DepartmentOut, DirectoryUserOut, RoomOut

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/rooms", response_model=list[RoomOut], summary="The five meeting rooms")
def list_rooms(db: DbSession) -> list[Room]:
    """Rooms in the order the grid shows them."""
    return list(db.scalars(select(Room).order_by(Room.display_order)).all())


@router.get(
    "/departments", response_model=list[DepartmentOut], summary="Departments"
)
def list_departments(db: DbSession) -> list[Department]:
    return list(
        db.scalars(select(Department).order_by(Department.display_order)).all()
    )


@router.get(
    "/directory",
    response_model=list[DirectoryUserOut],
    summary="Active directory members",
)
def list_directory(db: DbSession) -> list[DirectoryUserOut]:
    """Everyone who may host or attend a meeting.

    Deactivated accounts are excluded: D-04 restricts hosting to directory
    members, and a leaver is no longer one.
    """
    users = db.scalars(
        select(User)
        .where(User.is_active.is_(True))
        .options(selectinload(User.department))
        .order_by(User.full_name)
    ).all()

    return [
        DirectoryUserOut(
            id=user.id,
            full_name=user.full_name,
            email=user.email,
            department=user.department.name if user.department else None,
            role=user.role.value,
        )
        for user in users
    ]

"""Seed the reference data NM Meet cannot run without.

Runs on every single deploy, so it must be idempotent. It is:

* Rooms and departments are system-owned. They are inserted if missing and kept
  in step if their display attributes change, because no user can edit them.
* Directory users are inserted if their email is not already present and are
  never overwritten, because the directory is admin-maintained and will drift
  from this file the moment somebody changes a department or deactivates a
  leaver.

Nothing is ever deleted. Holidays are deliberately not seeded: reception and
admin maintain that table, and scripts/load_holidays.py imports a real list.

Run with:  python -m scripts.seed
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import configure_logging
from app.core.security import generate_password, hash_password
from app.database import SessionLocal
from app.models import Department, Room, User, UserRole

logger = logging.getLogger("scripts.seed")

# The five rooms, in the order the grid shows them. colour_var is the CSS custom
# property the approved frontend already uses for each room.
# id, name, display order, colour variable, minimum sensible party size.
ROOMS: tuple[tuple[str, str, int, str, int], ...] = (
    ("spark", "Spark", 1, "--spark", 4),
    ("power", "Power", 2, "--power", 7),
    ("pulse", "Pulse", 3, "--pulse", 3),
    ("ignite", "Ignite", 4, "--ignite", 2),
    ("switch", "Switch", 5, "--switch", 2),
)

DEPARTMENTS: tuple[tuple[str, int], ...] = (
    ("Sales", 1),
    ("HR", 2),
    ("Finance", 3),
    ("Operations", 4),
    ("IT", 5),
    ("Marketing", 6),
)

EMAIL_DOMAIN = "shreenm.com"

# The eight directory members from the approved prototype, plus the front desk
# and one administrator. The directory is admin-maintained: there is no HR sync,
# so this list is the starting point and the ADMIN user is who edits it after.
# Department assignments are settled: every department including HR has a member.
DIRECTORY: tuple[tuple[str, str, str, UserRole], ...] = (
    ("Priya Nair", "priya.nair", "IT", UserRole.EMPLOYEE),
    ("Rahul Mehta", "rahul.mehta", "Sales", UserRole.EMPLOYEE),
    ("Sana Qureshi", "sana.qureshi", "Operations", UserRole.EMPLOYEE),
    ("Vikram Rao", "vikram.rao", "Sales", UserRole.EMPLOYEE),
    ("Aditi Shah", "aditi.shah", "HR", UserRole.EMPLOYEE),
    ("Imran Sheikh", "imran.sheikh", "IT", UserRole.EMPLOYEE),
    ("Neha Kulkarni", "neha.kulkarni", "Marketing", UserRole.EMPLOYEE),
    ("Joseph Dsouza", "joseph.dsouza", "Finance", UserRole.EMPLOYEE),
    ("Reception Mumbai", "reception.mumbai", "Operations", UserRole.RECEPTION),
    ("IT Admin", "admin", "IT", UserRole.ADMIN),
)


def seed_departments(db: Session) -> dict[str, Department]:
    """Insert any missing department and return them all by name."""
    existing = {d.name: d for d in db.scalars(select(Department)).all()}

    for name, order in DEPARTMENTS:
        department = existing.get(name)
        if department is None:
            department = Department(name=name, display_order=order)
            db.add(department)
            existing[name] = department
            logger.info("Seeding department %s", name)
        elif department.display_order != order:
            department.display_order = order
            logger.info("Updating display order for department %s", name)

    db.flush()
    return existing


def seed_rooms(db: Session) -> None:
    """Insert or refresh the five rooms."""
    existing = {r.id: r for r in db.scalars(select(Room)).all()}

    for room_id, name, order, colour_var, min_people in ROOMS:
        room = existing.get(room_id)
        if room is None:
            db.add(
                Room(
                    id=room_id,
                    name=name,
                    display_order=order,
                    colour_var=colour_var,
                    min_people=min_people,
                )
            )
            logger.info("Seeding room %s", name)
            continue

        # Rooms are not user-editable, so the file is the source of truth.
        if (room.name, room.display_order, room.colour_var, room.min_people) != (
            name,
            order,
            colour_var,
            min_people,
        ):
            room.name = name
            room.display_order = order
            room.colour_var = colour_var
            room.min_people = min_people
            logger.info("Updating room %s", name)

    db.flush()


def seed_users(db: Session, departments: dict[str, Department]) -> None:
    """Insert any directory member whose email is not already present.

    Each new account gets a password. SEED_PASSWORD sets the same one for
    everybody, which is what you want on a laptop; leave it unset and each
    account gets its own random one. Either way the passwords are printed once,
    here, and stored only as bcrypt hashes. Nothing is ever written to a file.
    """
    existing_emails = set(db.scalars(select(User.email)).all())
    issued: list[tuple[str, str]] = []

    for full_name, local_part, department_name, role in DIRECTORY:
        email = f"{local_part}@{EMAIL_DOMAIN}"
        if email in existing_emails:
            continue

        password = settings.seed_password or generate_password()
        db.add(
            User(
                full_name=full_name,
                email=email,
                department_id=departments[department_name].id,
                role=role,
                is_active=True,
                password_hash=hash_password(password),
            )
        )
        issued.append((email, password))
        logger.info("Seeding user %s <%s> as %s", full_name, email, role.value)

    db.flush()

    if issued:
        # Printed once, to stdout, never committed and never stored in clear.
        rule = "=" * 66
        print(f"\n{rule}")
        print("  NM Meet sign-in details - shown once, not stored anywhere")
        print(rule)
        for email, password in issued:
            print(f"  {email:<34} {password}")
        print(f"{rule}\n", flush=True)


def run() -> None:
    """Seed everything in one transaction."""
    with SessionLocal() as db:
        departments = seed_departments(db)
        seed_rooms(db)
        seed_users(db, departments)
        db.commit()

    logger.info(
        "Seed complete: %d rooms, %d departments, %d directory users.",
        len(ROOMS),
        len(DEPARTMENTS),
        len(DIRECTORY),
    )


def main() -> int:
    configure_logging()
    try:
        run()
    except Exception:
        logger.exception("Seeding failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

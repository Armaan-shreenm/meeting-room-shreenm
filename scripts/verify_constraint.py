"""Prove the double-booking guarantee against a real PostgreSQL.

Spec section 7 names three layers and says only the third is a guarantee. This
script tests the third one — the ``no_double_booking`` EXCLUDE constraint — by
asking the database to store overlapping rows and requiring it to refuse.

An overlap must be rejected as ``psycopg2.errors.ExclusionViolation`` and nothing
else. A bare ``except`` would pass just as happily on a typo, a missing table or
a NOT NULL violation, and would prove nothing.

Every row this script writes is removed again, including on failure. It exits
non-zero if any check fails.

Usage:
    python -m scripts.verify_constraint
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import psycopg2.errors
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import booking_date_for, to_utc
from app.database import SessionLocal, engine
from app.models import (
    Booking,
    BookingStatus,
    Department,
    Room,
    User,
)

# A date far enough ahead that it cannot collide with anything real.
PROBE_DATE = "2099-06-15"
PROBE_TITLE_PREFIX = "verify_constraint probe"

# The rooms the specification's worked example uses (section 5 and tests T-01
# to T-04): Power holds the 13:00-15:00 booking, Pulse proves a different room
# is unaffected.
PROBE_ROOM = "power"
PROBE_OTHER_ROOM = "pulse"

PASS = "PASS"
FAIL = "FAIL"

_results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    """Print and remember one check."""
    status = PASS if ok else FAIL
    line = f"[{status}] {name}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)
    _results.append((status, name, detail))
    return ok


def at(hour: int, minute: int = 0) -> datetime:
    """A branch-local time on the probe date, converted to UTC for storage."""
    naive = datetime.fromisoformat(f"{PROBE_DATE}T{hour:02d}:{minute:02d}:00")
    return to_utc(naive)


def make_booking(
    room_id: str,
    start: datetime,
    end: datetime,
    department_id: int,
    user_id: int,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> Booking:
    return Booking(
        id=uuid.uuid4(),
        room_id=room_id,
        booking_date=booking_date_for(start),
        entry_time=start,
        exit_time=end,
        department_id=department_id,
        conducted_by=user_id,
        booked_by=user_id,
        title=f"{PROBE_TITLE_PREFIX} {room_id} {start:%H:%M}",
        status=status,
    )


def insert(db: Session, booking: Booking) -> None:
    """Insert and flush, so the constraint is evaluated now."""
    db.add(booking)
    db.flush()


def expect_exclusion_violation(db: Session, booking: Booking) -> str | None:
    """Insert expecting rejection. Returns the PostgreSQL error text, or None.

    ``None`` means the database accepted a row it should have refused, which is
    the one outcome that invalidates the whole design.
    """
    savepoint = db.begin_nested()
    try:
        db.add(booking)
        db.flush()
    except IntegrityError as exc:
        savepoint.rollback()
        if isinstance(exc.orig, psycopg2.errors.ExclusionViolation):
            return str(exc.orig).strip()
        raise AssertionError(
            "Insert was rejected, but not by the exclusion constraint. "
            f"Got {type(exc.orig).__name__}: {exc.orig}"
        ) from exc
    else:
        savepoint.rollback()
        return None


def cleanup(db: Session) -> int:
    """Remove every probe row. Safe to call more than once."""
    result = db.execute(
        delete(Booking).where(Booking.title.like(f"{PROBE_TITLE_PREFIX}%"))
    )
    db.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------- checks 1 & 2


def check_extension(db: Session) -> bool:
    present = db.scalar(
        select(func.count()).select_from(text("pg_extension")).where(
            text("extname = 'btree_gist'")
        )
    )
    return record(
        "1. btree_gist present in pg_extension",
        bool(present),
        "" if present else "Extension missing — the constraint cannot exist.",
    )


def check_constraint(db: Session) -> bool:
    definition = db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'no_double_booking'"
        )
    )
    return record(
        "2. no_double_booking present in pg_constraint",
        definition is not None,
        definition or "Constraint missing — nothing prevents a double booking.",
    )


# ------------------------------------------------------------- checks 3 to 8


def check_overlaps(db: Session, room: str, other_room: str, dept: int, user: int) -> bool:
    ok = True

    # 3 — the anchor booking.
    anchor = make_booking(room, at(13), at(15), dept, user)
    insert(db, anchor)
    ok &= record(f"3. {room} 13:00-15:00 inserted", True)

    # 4 — a genuine overlap must be refused by the database.
    clash = make_booking(room, at(14), at(16), dept, user)
    error_text = expect_exclusion_violation(db, clash)
    ok &= record(
        f"4. {room} 14:00-16:00 REJECTED as overlap",
        error_text is not None,
        error_text or "ACCEPTED — the constraint did not fire. Stop and fix this.",
    )

    # 5 — ends exactly when the anchor starts. Spec section 5, T-03.
    before = make_booking(room, at(12), at(13), dept, user)
    insert(db, before)
    ok &= record(f"5. {room} 12:00-13:00 inserted (back to back, ends at 13:00)", True)

    # 6 — starts exactly when the anchor ends. Spec section 5, T-04.
    after = make_booking(room, at(15), at(16), dept, user)
    insert(db, after)
    ok &= record(f"6. {room} 15:00-16:00 inserted (back to back, starts at 15:00)", True)

    # 7 — same window, different room. Spec section 1, T-02.
    elsewhere = make_booking(other_room, at(13), at(15), dept, user)
    insert(db, elsewhere)
    ok &= record(f"7. {other_room} 13:00-15:00 inserted (different room, same window)", True)

    # 8 — cancelling releases the window, because the constraint is scoped to
    # CONFIRMED. Spec section 9 point 4.
    anchor.status = BookingStatus.CANCELLED
    db.flush()
    replacement = make_booking(room, at(13), at(15), dept, user)
    insert(db, replacement)
    ok &= record(
        f"8. {room} 13:00-15:00 re-inserted after cancelling the original",
        True,
        "WHERE (status = 'CONFIRMED') scoping works.",
    )

    db.rollback()
    return bool(ok)


# ------------------------------------------------------------ checks 9 and 10


def check_migration_idempotent() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    already_at_head = "Running upgrade" not in (result.stdout + result.stderr)
    ok = result.returncode == 0 and already_at_head
    return record(
        "9. alembic upgrade head is a clean no-op on a second run",
        ok,
        f"exit={result.returncode}, re-ran a migration={not already_at_head}",
    )


def check_seed_idempotent(db: Session) -> bool:
    def counts() -> tuple[int, int, int]:
        return (
            db.scalar(select(func.count()).select_from(Room)) or 0,
            db.scalar(select(func.count()).select_from(Department)) or 0,
            db.scalar(select(func.count()).select_from(User)) or 0,
        )

    before = counts()
    result = subprocess.run(
        [sys.executable, "-m", "scripts.seed"], capture_output=True, text=True
    )
    db.expire_all()
    after = counts()

    ok = result.returncode == 0 and before == after
    return record(
        "10. scripts.seed creates no duplicates on a second run",
        ok,
        f"rooms/departments/users before={before} after={after}",
    )


def main() -> int:
    print(f"Verifying against {engine.url.render_as_string(hide_password=True)}\n")

    exit_code = 0
    with SessionLocal() as db:
        try:
            room = db.get(Room, PROBE_ROOM)
            other = db.get(Room, PROBE_OTHER_ROOM)
            department = db.scalar(select(Department).order_by(Department.id))
            user = db.scalar(select(User).order_by(User.id))

            if not all((room, other, department, user)):
                print(
                    f"[FAIL] Reference data missing (need rooms {PROBE_ROOM!r} and "
                    f"{PROBE_OTHER_ROOM!r}). Run scripts.seed first."
                )
                return 1

            ok = check_extension(db)
            ok &= check_constraint(db)
            ok &= check_overlaps(db, room.id, other.id, department.id, user.id)
            ok &= check_migration_idempotent()
            ok &= check_seed_idempotent(db)

            if not ok:
                exit_code = 1
        finally:
            removed = cleanup(db)
            print(f"\nCleanup: {removed} probe booking(s) removed.")

    passed = sum(1 for status, _, _ in _results if status == PASS)
    total = len(_results)
    print(f"{passed}/{total} checks passed.")

    if exit_code:
        print("\nFAILED — do not build booking logic on this schema.")
    else:
        print("\nAll checks passed. The database refuses double bookings.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""Import declared holidays from a CSV.

Reception and admin maintain the holiday list. Nothing invented is ever seeded,
because a wrong holiday silently closes the office on a working day, so the
dates have to come from the business.

The weekly Sunday closure is NOT stored here. It is computed from
``settings.closed_weekdays`` (D-03: Monday to Saturday working, Sunday closed).
Putting every Sunday in this table would be tens of rows a year describing a rule
that is one line of code.

CSV format - a header row is optional and skipped if present:

    date,name
    2027-01-26,Republic Day
    2027-03-25,Holi

Dates are ISO ``YYYY-MM-DD``. Re-importing the same file is safe: a date already
present is updated if its name changed and left alone otherwise. Nothing is ever
deleted, so removing a holiday is a deliberate act, not a side effect of editing
a spreadsheet.

Usage:
    python -m scripts.load_holidays holidays_2027.csv
    python -m scripts.load_holidays holidays_2027.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import configure_logging
from app.database import SessionLocal
from app.models import Holiday

logger = logging.getLogger("scripts.load_holidays")

_DATE_FORMAT = "%Y-%m-%d"
_HEADER_VALUES = {"date", "day", "holiday_date"}


class HolidayCsvError(ValueError):
    """The CSV could not be read as a holiday list."""


def parse_csv(path: Path) -> list[tuple[date, str]]:
    """Read the file into (date, name) pairs, reporting the offending line."""
    if not path.is_file():
        raise HolidayCsvError(f"No such file: {path}")

    rows: list[tuple[date, str]] = []
    seen: dict[date, str] = {}

    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, fields in enumerate(csv.reader(handle), start=1):
            if not fields or all(not field.strip() for field in fields):
                continue

            if len(fields) < 2:
                raise HolidayCsvError(
                    f"{path}:{line_number}: expected 'date,name', got {fields!r}"
                )

            raw_date = fields[0].strip()
            name = fields[1].strip()

            # Skip an optional header row.
            if line_number == 1 and raw_date.lower() in _HEADER_VALUES:
                continue

            try:
                parsed = datetime.strptime(raw_date, _DATE_FORMAT).date()
            except ValueError as exc:
                raise HolidayCsvError(
                    f"{path}:{line_number}: {raw_date!r} is not a YYYY-MM-DD date"
                ) from exc

            if not name:
                raise HolidayCsvError(
                    f"{path}:{line_number}: {parsed.isoformat()} has no name"
                )

            if parsed in seen:
                raise HolidayCsvError(
                    f"{path}:{line_number}: {parsed.isoformat()} appears twice "
                    f"({seen[parsed]!r} and {name!r})"
                )

            seen[parsed] = name
            rows.append((parsed, name))

    if not rows:
        raise HolidayCsvError(f"{path} contains no holidays")

    return rows


def load(db: Session, rows: list[tuple[date, str]]) -> tuple[int, int, int]:
    """Insert or update holidays. Returns (added, renamed, unchanged)."""
    existing = {h.date: h for h in db.scalars(select(Holiday)).all()}
    added = renamed = unchanged = 0

    for holiday_date, name in rows:
        current = existing.get(holiday_date)

        if current is None:
            db.add(Holiday(date=holiday_date, name=name))
            logger.info("Adding %s - %s", holiday_date.isoformat(), name)
            added += 1
        elif current.name != name:
            logger.info(
                "Renaming %s - %r becomes %r",
                holiday_date.isoformat(),
                current.name,
                name,
            )
            current.name = name
            renamed += 1
        else:
            unchanged += 1

    db.flush()
    return added, renamed, unchanged


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import declared holidays from a CSV of date,name."
    )
    parser.add_argument("csv_path", type=Path, help="Path to the CSV file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report, then roll back without writing.",
    )
    args = parser.parse_args()

    configure_logging()

    try:
        rows = parse_csv(args.csv_path)
    except HolidayCsvError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Read %d holidays from %s", len(rows), args.csv_path)

    try:
        with SessionLocal() as db:
            added, renamed, unchanged = load(db, rows)
            if args.dry_run:
                db.rollback()
                logger.info("Dry run - rolled back, nothing written.")
            else:
                db.commit()
    except Exception:
        logger.exception("Holiday import failed")
        return 1

    logger.info(
        "Holidays: %d added, %d renamed, %d unchanged.", added, renamed, unchanged
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

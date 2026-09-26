"""Build the weekly usage report, and send it only when asked.

Safe by default: without ``--send`` nothing leaves the machine. The numbers
are printed and the email is written to an HTML file to open in a browser.

Usage:
    python -m scripts.usage_report                                  # last working week, preview only
    python -m scripts.usage_report --from 2026-09-21 --to 2026-09-25
    python -m scripts.usage_report --send                           # to USAGE_REPORT_RECIPIENTS
    python -m scripts.usage_report --send --recipient me@shreenm.com  # to that address only

This is what a scheduler runs every Sunday - cron on a server, an EventBridge
schedule starting an ECS task, a Render cron job. Exits non-zero if the report
could not be built or sent, so the scheduler sees the failure.
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from datetime import date
from pathlib import Path

from app.config import settings
from app.core.logging import configure_logging
from app.database import SessionLocal
from app.services import usage_report
from app.services.notifications import LOGO_CID
from app.services.transports import LOGO_PATH

logger = logging.getLogger("scripts.usage_report")

DEFAULT_PREVIEW_FILE = Path("usage-report-preview.html")


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def _browser_html(html: str) -> str:
    """The email as a browser can show it: the cid: logo inlined as data."""
    try:
        logo = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:
        return html
    return html.replace(f"cid:{LOGO_CID}", f"data:image/png;base64,{logo}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", type=_day, help="first day, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", type=_day, help="last day, YYYY-MM-DD")
    parser.add_argument(
        "--send",
        action="store_true",
        help="actually send it; without this nothing is sent",
    )
    parser.add_argument(
        "--recipient",
        action="append",
        help="send only to this address instead of USAGE_REPORT_RECIPIENTS (repeatable)",
    )
    parser.add_argument(
        "--preview-file",
        type=Path,
        default=DEFAULT_PREVIEW_FILE,
        help=f"where the HTML preview is written (default {DEFAULT_PREVIEW_FILE})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()

    if (args.start is None) != (args.end is None):
        logger.error("Give both --from and --to, or neither.")
        return 2

    start, end = (args.start, args.end) if args.start else usage_report.default_period()
    if end < start:
        logger.error("--to must be on or after --from.")
        return 2

    recipients = args.recipient
    if recipients:
        domain = "@" + settings.allowed_email_domain.lower()
        outside = [r for r in recipients if not r.lower().endswith(domain)]
        if outside:
            logger.error("Refusing to send outside %s: %s", domain, ", ".join(outside))
            return 2

    with SessionLocal() as db:
        report = usage_report.build_report(db, start, end)

    note = usage_report.default_preview_note()
    print(f"Period               {usage_report.period_label(start, end)}")
    print(f"People who signed in {report.people_signed_in}")
    print(f"Meetings scheduled   {report.meetings_scheduled}")

    args.preview_file.write_text(
        _browser_html(usage_report.render_html(report, note)), encoding="utf-8"
    )
    print(f"Preview written to   {args.preview_file.resolve()}")

    if not args.send:
        print("Not sent. Add --send to send it.")
        return 0

    try:
        sent_to = usage_report.send_report(report, recipients, preview_note=note)
    except usage_report.ReportNotConfigured as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        logger.exception("Sending the usage report failed")
        return 1

    print(f"Sent to              {', '.join(sent_to)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

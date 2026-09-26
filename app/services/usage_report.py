"""The weekly usage report - how much NM Meet is actually used.

Two numbers, for one period of branch-local days:

* **People who signed in.** The schema keeps no sign-in history: ``users``
  holds only ``last_login_at``, which a later sign-in overwrites. So the count
  is every distinct person with evidence of being signed in during the period -
  a ``last_login_at`` inside it, a booking made inside it, or an audited action
  (cancel, edit, reply) inside it. Every one of those needs a session while
  SIGN_IN_REQUIRED is on. The one person it can miss is somebody who signed in
  during the period, did nothing, and signed in again after it ended.
* **Meetings scheduled.** Bookings made during the period, by ``created_at``,
  that were not cancelled afterwards. Exact: bookings are never deleted.

Nothing here depends on where NM Meet is hosted. The report is built and sent
by :func:`send_report`, which the CLI (``scripts/usage_report.py``) and the
token-protected endpoint (``app/api/reports.py``) both call, so any scheduler
that can run a command or make an HTTP request can drive it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape

from sqlalchemy import func, select, union
from sqlalchemy.orm import Session

from app.config import settings
from app.core import time as timeutil
from app.models import AuditLog, Booking, BookingStatus, User
from app.services.notifications import (
    BRAND,
    BRAND_DARK,
    INK,
    INK_2,
    LINE,
    LOGO_CID,
    MUTED,
    PAGE,
    SUNK,
)
from app.services.transports import compose, select_transport

logger = logging.getLogger(__name__)

# Monday=0 ... Sunday=6. The report goes out on Sunday, the closed day, and
# covers the working week that has just ended: Monday to Saturday.
_MONDAY = 0
_SUNDAY = 6
_WORKING_DAYS = 6

# Shown above the numbers whenever they did not come from production, so a
# preview can never be mistaken for the real figures.
PREVIEW_NOTE = (
    "Preview from a {environment} database. These are not production figures."
)


class ReportNotConfigured(RuntimeError):
    """There is nobody to send the report to."""


@dataclass(frozen=True)
class UsageReport:
    """The two numbers for one period of branch-local days, inclusive."""

    start: date
    end: date
    people_signed_in: int
    meetings_scheduled: int


# ------------------------------------------------------------------- period


def default_period(today: date | None = None) -> tuple[date, date]:
    """The last complete working week, Monday to Saturday.

    Run on a Sunday, that is the week ending yesterday. Run on any other day,
    it is the previous week, so a late or repeated run reports the same period
    instead of a half-finished one.
    """
    today = today or timeutil.local_today()
    if today.weekday() == _SUNDAY:
        start = today - timedelta(days=_SUNDAY - _MONDAY)
    else:
        start = today - timedelta(days=today.weekday() + 7)
    return start, start + timedelta(days=_WORKING_DAYS - 1)


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """UTC instants for local midnight at ``start`` and after ``end``."""
    return (
        timeutil.local_datetime(start, 0),
        timeutil.local_datetime(end + timedelta(days=1), 0),
    )


# ------------------------------------------------------------------ numbers


def count_people_signed_in(db: Session, start: date, end: date) -> int:
    """Distinct people with evidence of a session during the period."""
    since, until = _bounds(start, end)

    evidence = [
        select(User.id.label("user_id")).where(
            User.last_login_at >= since, User.last_login_at < until
        )
    ]

    # With sign-in off, booked_by is whoever the form named and the audit row
    # has no actor, so neither says anything about who signed in.
    if settings.sign_in_required:
        evidence.append(
            select(Booking.booked_by.label("user_id")).where(
                Booking.created_at >= since, Booking.created_at < until
            )
        )
        evidence.append(
            select(AuditLog.actor_id.label("user_id")).where(
                AuditLog.actor_id.is_not(None),
                AuditLog.created_at >= since,
                AuditLog.created_at < until,
            )
        )

    people = union(*evidence).subquery()
    return db.scalar(select(func.count()).select_from(people)) or 0


def count_meetings_scheduled(db: Session, start: date, end: date) -> int:
    """Bookings made during the period and still standing."""
    since, until = _bounds(start, end)
    return (
        db.scalar(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.created_at >= since,
                Booking.created_at < until,
                Booking.status != BookingStatus.CANCELLED,
            )
        )
        or 0
    )


def build_report(db: Session, start: date, end: date) -> UsageReport:
    if end < start:
        raise ValueError("The report period must end on or after the day it starts.")
    return UsageReport(
        start=start,
        end=end,
        people_signed_in=count_people_signed_in(db, start, end),
        meetings_scheduled=count_meetings_scheduled(db, start, end),
    )


# ---------------------------------------------------------------- rendering


def period_label(start: date, end: date) -> str:
    """"Mon 21 Sep to Fri 25 Sep 2026", with a year on both ends only if needed."""
    first = start.strftime("%a %d %b") if start.year == end.year else start.strftime("%a %d %b %Y")
    return f"{first} to {end.strftime('%a %d %b %Y')}"


def subject_for(report: UsageReport, preview: bool) -> str:
    subject = f"NM Meet weekly usage: {period_label(report.start, report.end)}"
    return f"[Preview] {subject}" if preview else subject


def render_text(report: UsageReport, preview_note: str | None) -> str:
    lines = [
        f"NM Meet weekly usage, {settings.branch_name}",
        period_label(report.start, report.end),
        "",
    ]
    if preview_note:
        lines += [preview_note, ""]
    lines += [
        f"People who signed in   {report.people_signed_in}",
        f"Meetings scheduled     {report.meetings_scheduled}",
        "",
        "People who signed in: everyone who signed in, booked, cancelled or",
        "changed a booking during the period, counted once each.",
        "Meetings scheduled: bookings made during the period, not counting",
        "any that were cancelled.",
    ]
    return "\n".join(lines)


TEMPLATE = """<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:%(page)s;">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"
       style="background:%(page)s;padding:16px 10px;">
<tr><td align="center">

<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
       style="max-width:560px;width:100%%;background:#FFFFFF;border:1px solid %(line)s;
              border-radius:12px;overflow:hidden;">

  <tr><td style="padding:16px 20px;border-bottom:1px solid %(line)s;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="padding-right:10px;">
        <img src="cid:%(logo_cid)s" width="30" height="30" alt="Shree NM"
             style="display:block;border:0;"></td>
      <td style="font-family:%(font)s;font-size:17px;font-weight:bold;color:%(ink)s;
                 letter-spacing:-0.2px;white-space:nowrap;">Shree NM</td>
      <td style="padding-left:8px;">
        <span style="font-family:%(font)s;font-size:9px;font-weight:bold;color:#FFFFFF;
                     background:%(brand)s;padding:3px 6px;border-radius:3px;
                     letter-spacing:1.2px;">REPORT</span></td>
    </tr></table>
  </td></tr>

  <tr><td align="center" style="padding:20px 20px 0 20px;">
    <div style="font-family:%(font)s;font-size:19px;line-height:1.3;font-weight:bold;
                color:%(ink)s;">NM Meet Weekly Usage</div>
    <div style="font-family:%(font)s;font-size:13px;color:%(muted)s;padding-top:7px;">
      %(branch)s branch &middot; %(period)s</div>
  </td></tr>

  %(preview)s

  <tr><td style="padding:16px 20px 0 20px;">
    <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td width="50%%" valign="top" style="padding-right:6px;">%(tile_people)s</td>
        <td width="50%%" valign="top" style="padding-left:6px;">%(tile_meetings)s</td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="padding:16px 20px 20px 20px;">
    <div style="font-family:%(font)s;font-size:12px;line-height:1.5;color:%(muted)s;">
      <b style="color:%(ink2)s;">People who signed in</b> counts everyone who
      signed in, booked, cancelled or changed a booking during the period, once each.<br>
      <b style="color:%(ink2)s;">Meetings scheduled</b> counts bookings made
      during the period, not counting any that were cancelled.
    </div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def _tile(value: int, label: str) -> str:
    """One big number in the grid's sunk box - the booking mail's detail table look."""
    font = "Arial,Helvetica,sans-serif"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
        f' style="background:{SUNK};border:1px solid {LINE};border-radius:8px;">'
        f'<tr><td align="center" style="padding:18px 10px;">'
        f'<div style="font-family:{font};font-size:34px;line-height:1;font-weight:bold;'
        f'color:{BRAND_DARK};">{value}</div>'
        f'<div style="font-family:{font};font-size:13px;color:{INK_2};padding-top:8px;">'
        f"{escape(label)}</div>"
        f"</td></tr></table>"
    )


def render_html(report: UsageReport, preview_note: str | None) -> str:
    font = "Arial,Helvetica,sans-serif"
    preview = ""
    if preview_note:
        preview = (
            '<tr><td style="padding:14px 20px 0 20px;">'
            f'<div style="font-family:{font};font-size:12px;color:{INK};'
            f"background:#FFF4D6;border:1px solid {BRAND};border-radius:6px;"
            f'padding:8px 10px;text-align:center;">{escape(preview_note)}</div>'
            "</td></tr>"
        )

    return TEMPLATE % {
        "page": PAGE,
        "line": LINE,
        "ink": INK,
        "ink2": INK_2,
        "muted": MUTED,
        "brand": BRAND,
        "font": font,
        "logo_cid": LOGO_CID,
        "branch": escape(settings.branch_name),
        "period": escape(period_label(report.start, report.end)),
        "preview": preview,
        "tile_people": _tile(report.people_signed_in, "People who signed in"),
        "tile_meetings": _tile(report.meetings_scheduled, "Meetings scheduled"),
    }


def default_preview_note() -> str | None:
    """Anything but production gets the banner."""
    if settings.is_production:
        return None
    return PREVIEW_NOTE.format(environment=settings.environment)


def compose_messages(
    report: UsageReport, recipients: list[str], preview_note: str | None
) -> list[EmailMessage]:
    """One message per recipient, so nobody sees who else receives it."""
    subject = subject_for(report, preview=preview_note is not None)
    text = render_text(report, preview_note)
    html = render_html(report, preview_note)
    return [
        compose(
            subject=subject,
            text=text,
            html=html,
            to_email=address,
            to_name="",
            from_email=settings.smtp_from_email,
            from_name=settings.smtp_from_name,
        )
        for address in recipients
    ]


# ------------------------------------------------------------------ sending


def send_report(
    report: UsageReport,
    recipients: list[str] | None = None,
    *,
    transport=None,
    preview_note: str | None = None,
) -> list[str]:
    """Send the report and return who it went to.

    ``recipients`` defaults to USAGE_REPORT_RECIPIENTS and ``transport`` to
    whatever the environment configures for booking mail. Raises
    :class:`ReportNotConfigured` when there is nobody to send to, and lets a
    transport failure propagate: a report that did not go must not look sent.
    """
    if recipients is None:
        recipients = settings.usage_report_recipient_list
    if not recipients:
        raise ReportNotConfigured(
            "USAGE_REPORT_RECIPIENTS is empty, so there is nobody to send the report to."
        )

    if transport is None:
        transport = select_transport()

    for mail in compose_messages(report, recipients, preview_note):
        transport.deliver(mail)
        logger.info(
            "Sent usage report %s..%s to %s via %s",
            report.start,
            report.end,
            mail["To"],
            transport.name,
        )
    return recipients

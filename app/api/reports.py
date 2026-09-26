"""The weekly usage report, triggered over HTTP.

For schedulers that can make a request but cannot run a command in NM Meet's
environment - an EventBridge API destination, cron-job.org, a GitHub Actions
schedule. Where a command can run, ``python -m scripts.usage_report --send``
does the same thing.

Not a user endpoint: it is authorised by a shared secret in a header, not by a
session, and it does not exist at all until USAGE_REPORT_TOKEN is set. The
recipients always come from configuration, never from the request, so the token
cannot be used to send mail anywhere else.
"""

from __future__ import annotations

import hmac
import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services import usage_report

logger = logging.getLogger(__name__)

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]

REPORT_TOKEN_HEADER = "X-Report-Token"

# A weekly report; a month is generous and stops a typo asking for years.
MAX_PERIOD_DAYS = 31


class UsageReportRequest(BaseModel):
    """Both omitted means the last complete working week."""

    start: date | None = None
    end: date | None = None


class UsageReportResult(BaseModel):
    start: date
    end: date
    people_signed_in: int
    meetings_scheduled: int
    sent_to: list[str]


def _authorise(token: str | None) -> None:
    if not settings.usage_report_token:
        # Switched off: answer as though the route were never registered.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not token or not hmac.compare_digest(token, settings.usage_report_token):
        logger.warning("Usage report requested with a wrong or missing token")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That report token is not valid.",
        )


@router.post(
    "/reports/weekly-usage",
    response_model=UsageReportResult,
    summary="Build and send the weekly usage report",
    include_in_schema=False,
)
def send_weekly_usage(
    db: DbSession,
    x_report_token: Annotated[str | None, Header(alias=REPORT_TOKEN_HEADER)] = None,
    payload: UsageReportRequest | None = None,
) -> UsageReportResult:
    _authorise(x_report_token)

    start, end = usage_report.default_period()
    if payload is not None and (payload.start or payload.end):
        if payload.start is None or payload.end is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Give both a start and an end date, or neither.",
            )
        start, end = payload.start, payload.end

    if end < start or end - start > timedelta(days=MAX_PERIOD_DAYS - 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The period must run forwards and cover at most {MAX_PERIOD_DAYS} days.",
        )

    report = usage_report.build_report(db, start, end)
    try:
        sent_to = usage_report.send_report(
            report, preview_note=usage_report.default_preview_note()
        )
    except usage_report.ReportNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return UsageReportResult(
        start=report.start,
        end=report.end,
        people_signed_in=report.people_signed_in,
        meetings_scheduled=report.meetings_scheduled,
        sent_to=sent_to,
    )

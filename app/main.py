"""FastAPI application.

One process serves everything. The API lives under ``/api`` and the approved
frontend is served as static files from ``/``, so the browser calls the API with
relative paths and there is no second origin, no CORS in production and no
separate frontend host to deploy.

Every error the user can see is turned into a string from
:mod:`app.core.messages` by the handlers below. Spec section 10 rules out
"Invalid selection" and "Booking failed", and that includes the messages a
framework would otherwise generate on its own.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.config import settings
from app.core import messages
from app.core.errors import ConflictError, NmMeetError
from app.core.logging import configure_logging
from app.core.middleware import (
    REQUEST_ID_HEADER,
    RateLimitMiddleware,
    RequestIdMiddleware,
    request_id_var,
)
from app.services import notifications
from app.services.transports import configure_transport

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger(__name__)

# Which section 10 message a malformed or missing field should produce, so the
# framework never speaks to the user in its own words.
_FIELD_MESSAGES = {
    "department_id": messages.DEPARTMENT_REQUIRED,
    "conducted_by": messages.CONDUCTOR_REQUIRED,
    "entry": messages.BAD_TIME,
    "exit": messages.BAD_TIME,
    "date": messages.BAD_DATE,
}

_PYDANTIC_PREFIX = "Value error, "


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Log the resolved configuration once, on a cold start.

    No connection pool is warmed and no background thread is started: Render may
    stop and restart this process at any time, so nothing that matters may live
    only in memory.
    """
    configure_logging()
    transport = configure_transport()
    logger.info(
        "NM Meet %s starting - environment=%s branch=%s timezone=%s hours=%s-%s "
        f"notifications={transport}",
        __version__,
        settings.environment,
        settings.branch_name,
        settings.tz,
        settings.open_time.isoformat(timespec="minutes"),
        settings.close_time.isoformat(timespec="minutes"),
    )
    yield

    # Messages committed as QUEUED are on a daemon thread that dies with the
    # process. Give them a moment to go out rather than losing them to a
    # redeploy; anything still queued stays QUEUED in the log, which is true.
    notifications.drain(timeout=10.0)
    logger.info("NM Meet shutting down")


app = FastAPI(
    title="NM Meet",
    description=(
        "Meeting room booking for Shree NM, Mumbai branch. "
        "Five rooms, per-room availability, no double bookings."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# Middleware runs bottom-up, so RequestIdMiddleware is added last and therefore
# runs first: everything below it, including a rate-limit refusal, is logged
# with an id.
app.add_middleware(
    RateLimitMiddleware,
    limit=settings.rate_limit_bookings,
    window_seconds=settings.rate_limit_window_seconds,
)
app.add_middleware(RequestIdMiddleware)

# Permissive in development, where the frontend may be opened from a file or a
# second port. Effectively unused in production: the API and the page it serves
# share one origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(NmMeetError)
async def handle_domain_error(request: Request, exc: NmMeetError) -> JSONResponse:
    """Domain refusals already carry a section 10 message."""
    body: dict[str, object] = {"detail": exc.detail}
    if isinstance(exc, ConflictError):
        body["free_rooms"] = exc.free_rooms
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def handle_request_validation(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Translate Pydantic's complaints into the specification's wording.

    A body that fails schema validation would otherwise reach the user as
    "Input should be a valid date", which section 10 forbids.
    """
    detail = messages.BAD_DATE
    for error in exc.errors():
        location = [part for part in error.get("loc", ()) if isinstance(part, str)]
        field = location[-1] if location else ""

        if field in _FIELD_MESSAGES:
            detail = _FIELD_MESSAGES[field]
            break

        raw = str(error.get("msg", ""))
        if raw.startswith(_PYDANTIC_PREFIX):
            # A ValueError raised by one of our own validators.
            detail = raw[len(_PYDANTIC_PREFIX) :]
            break
    else:
        logger.info("Unmapped request validation error: %s", exc.errors())

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": detail}
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """The catch-all. A stack trace never reaches a user.

    The full traceback goes to stdout with the request id; the user gets a
    sentence and that same id to quote. Anything else either leaks the schema
    and file paths, or leaves the user with nothing to report.
    """
    request_id = request_id_var.get()
    logger.exception(
        "Unhandled error on %s %s (request id=%s)",
        request.method,
        request.url.path,
        request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": messages.UNEXPECTED_ERROR.format(request_id=request_id)},
        headers={REQUEST_ID_HEADER: request_id},
    )


# API first. The static mount below is a catch-all and would otherwise swallow
# these paths.
app.include_router(api_router, prefix="/api")

# The frontend. ``html=True`` serves index.html for "/" and returns a plain 404
# for anything unknown.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    # Local convenience only. In production start.sh runs uvicorn directly.
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=not settings.is_production,
    )

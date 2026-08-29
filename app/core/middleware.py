"""Request ids and rate limiting.

Both are deliberately small and in-process, because the deployment target is one
Render web service on one worker. Neither holds anything correctness depends on:
lose the rate-limit counters on a restart and the worst case is that a burst is
allowed through, which is a far better failure than refusing real bookings.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from contextvars import ContextVar

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.core import messages
from app.core.security import SESSION_COOKIE, read_session

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Read by the log filter so every line of a request carries the same id.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Rate-limit counters, module level so they can be inspected and cleared. The
# process is the whole scope: one Render worker, one set of counters.
_rate_hits: dict[str, deque[float]] = defaultdict(deque)


def reset_rate_limits() -> None:
    """Forget every counter. Used by the test suite between cases."""
    _rate_hits.clear()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Give every request an id, echo it back, and log how it went.

    An id supplied by an upstream proxy is honoured, so a request can be traced
    across hops.
    """

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming or uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        # One line per request, on stdout, where Render's log stream reads it.
        logger.info(
            'request id=%s %s %s -> %s %.1fms',
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A fixed-window limiter on the paths that create or change bookings.

    Reads are never limited: the grid polls availability every minute and
    throttling that would break the screen for no benefit.

    In-process and per-worker. On the free plan there is exactly one worker, so
    that is the whole limit. If NM Meet ever runs more than one, this becomes
    approximate and should move to the database or Redis - noted in HANDOVER.md
    rather than pretended otherwise.
    """

    def __init__(self, app, limit: int, window_seconds: int) -> None:
        super().__init__(app)
        self.limit = limit
        self.window = window_seconds

    def _key(self, request: Request) -> str:
        """Per signed-in user where there is one, per client address otherwise.

        Keyed on the user id inside the session, not on the token itself: a
        token changes every time somebody signs in, and keying on it would hand
        an attacker a fresh allowance for the price of one login.
        """
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            user_id = read_session(token)
            if user_id is not None:
                return f"u:{user_id}"

        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    def _limited(self, key: str) -> bool:
        now = time.monotonic()
        hits = _rate_hits[key]

        while hits and now - hits[0] > self.window:
            hits.popleft()

        if len(hits) >= self.limit:
            return True

        hits.append(now)
        return False

    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        if not request.url.path.startswith("/api/bookings"):
            return await call_next(request)

        key = self._key(request)
        if self._limited(key):
            logger.warning(
                "Rate limit hit for %s on %s %s", key, request.method, request.url.path
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": messages.TOO_MANY_REQUESTS},
                headers={"Retry-After": str(self.window)},
            )

        return await call_next(request)

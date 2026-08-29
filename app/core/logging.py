"""Logging configuration.

The Render filesystem is ephemeral, so nothing is ever written to a file. Every
log line goes to stdout, where Render's log stream picks it up.
"""

from __future__ import annotations

import logging
import sys

from app.config import settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id, or "-" outside one.

    Structured enough to grep without a JSON logging dependency: every line of
    one request shares an id, so a report of "reference a1b2c3d4" finds the
    whole story in Render's log stream.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Imported lazily: middleware imports config, which must not import this.
        from app.core.middleware import request_id_var

        record.request_id = request_id_var.get()
        return True


def configure_logging() -> None:
    """Send application and uvicorn logs to stdout at the configured level."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; make them defer to the root handler so
    # there is exactly one line per event on stdout.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)

    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.sql_echo else logging.WARNING
    )

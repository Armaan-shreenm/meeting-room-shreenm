"""Actor resolution.

Real authentication is Phase 5. This module is the seam it drops into.

Every endpoint from Phase 2 onward takes the acting user from
:func:`get_current_user` and never from the request body. That matters because
spec section 9 decides who may cancel or edit what by comparing the actor against
``booked_by`` and ``conducted_by`` — a body field would let a caller claim to be
somebody else and act on their meeting.

Today the actor comes from an ``X-User-Email`` header. **The
``DEV_USER_EMAIL`` fallback applies only outside production.** In production a
request with no header is rejected with 401, never silently attributed to a
default person: an unauthenticated booking made in somebody else's name is worse
than a failed request.

When SSO arrives, only the body of this function changes — it starts reading a
verified session or token instead of a header, and every call site keeps working.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.messages import (
    ACTOR_DEACTIVATED,
    ACTOR_NOT_IDENTIFIED,
    ACTOR_UNKNOWN,
)
from app.database import get_db
from app.models.user import User

logger = logging.getLogger(__name__)


def _resolve_email(x_user_email: str | None) -> str:
    """Work out which directory address is acting.

    Outside production an absent header falls back to ``settings.dev_user_email``
    so the API is usable from a browser or curl. In production there is no
    fallback at all.
    """
    header_email = (x_user_email or "").strip().lower()

    if header_email:
        return header_email

    if settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ACTOR_NOT_IDENTIFIED,
        )

    fallback = settings.dev_user_email.strip().lower()

    if not fallback:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ACTOR_NOT_IDENTIFIED,
        )

    logger.debug(
        "No X-User-Email header; using DEV_USER_EMAIL=%s (environment=%s)",
        fallback,
        settings.environment,
    )
    return fallback


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    x_user_email: Annotated[
        str | None,
        Header(
            alias="X-User-Email",
            description=(
                "Directory email of the acting user. Placeholder for real "
                "authentication, which arrives in Phase 5. Required in "
                "production; outside production it falls back to DEV_USER_EMAIL."
            ),
        ),
    ] = None,
) -> User:
    """Resolve the acting user, or refuse the request.

    401 when nobody is identified or the address is not in the directory, 403
    when the account is deactivated. Every message names what to do next, because
    spec section 10 rules out "Invalid selection" style errors.
    """
    email = _resolve_email(x_user_email)

    user = db.scalar(select(User).where(func.lower(User.email) == email))

    if user is None:
        logger.warning("Rejected request from unknown address %s", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ACTOR_UNKNOWN.format(email=email),
        )

    if not user.is_active:
        logger.warning("Rejected request from deactivated account %s", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ACTOR_DEACTIVATED.format(email=email),
        )

    return user


# Endpoints annotate their actor parameter with this, so the seam is visible at
# every call site: `actor: CurrentUser`.
CurrentUser = Annotated[User, Depends(get_current_user)]

"""Actor resolution and authorisation.

:func:`get_current_user` is the seam every endpoint depends on. Phases 2 to 4
resolved the actor from an ``X-User-Email`` header; this phase replaced the body
of that function with a signed session cookie. **No call site changed.**

Section 9 decides what somebody may do by comparing them against ``booked_by``
and ``conducted_by``, so the actor must never come from a request body. The
frontend hides what a user cannot do; the server refuses it regardless of what
the frontend showed.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.messages import (
    ACTOR_DEACTIVATED,
    ACTOR_NOT_IDENTIFIED,
    ACTOR_UNKNOWN,
    CSRF_FAILED,
)
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    csrf_matches,
    read_session,
)
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

# Requests that cannot change anything do not need a CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _load_active_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)

    if user is None:
        logger.warning("Session referenced user %s, who no longer exists", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ACTOR_UNKNOWN.format(email="That account"),
        )

    if not user.is_active:
        logger.warning("Rejected request from deactivated account %s", user.email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ACTOR_DEACTIVATED.format(email=user.email),
        )

    return user


def _check_csrf(request: Request) -> None:
    """Double-submit CSRF check on every state-changing request.

    SameSite=Lax already blocks a cross-site form POST in current browsers, but
    it is a single control and it is the browser's to enforce. The token makes
    the check ours.
    """
    if request.method in SAFE_METHODS:
        return

    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)

    if not csrf_matches(cookie, header):
        logger.warning(
            "CSRF check failed for %s %s", request.method, request.url.path
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_FAILED
        )


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Resolve the acting user from the session cookie, or refuse the request.

    401 when nobody is signed in or the session is expired, tampered with or
    points at a deleted account. 403 when the account is deactivated, and 403
    when a state-changing request arrives without a matching CSRF token.
    """
    token = request.cookies.get(SESSION_COOKIE)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ACTOR_NOT_IDENTIFIED
        )

    user_id = read_session(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ACTOR_NOT_IDENTIFIED
        )

    _check_csrf(request)
    return _load_active_user(db, user_id)


def get_optional_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    """The signed-in user, or None. Used by the login page to skip the form.

    Never runs the CSRF check: it answers a GET and refusing here would make the
    login page unreachable for anyone holding a stale cookie.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    user_id = read_session(token)
    if user_id is None:
        return None

    user = db.get(User, user_id)
    return user if user is not None and user.is_active else None


# Endpoints annotate their actor parameter with this, so the seam is visible at
# every call site: `actor: CurrentUser`.
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated["User | None", Depends(get_optional_user)]

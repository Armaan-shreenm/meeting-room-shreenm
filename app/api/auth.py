"""Sign in, sign out, and who am I.

Login is deliberately generous about time and stingy about information: a wrong
address and a wrong password produce the same message, so the form cannot be
used to discover who has an account.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core import messages
from app.core import time as timeutil
from app.core.auth import CurrentUser
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    csrf_cookie_kwargs,
    csrf_matches,
    generate_csrf_token,
    issue_session,
    session_cookie_kwargs,
    verify_password,
)
from app.database import get_db
from app.models import User
from app.schemas.reference import DirectoryUserOut

logger = logging.getLogger(__name__)

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


def _as_directory_user(user: User) -> DirectoryUserOut:
    return DirectoryUserOut(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        department=user.department.name if user.department else None,
        role=user.role.value,
    )


@router.post(
    "/auth/login",
    response_model=DirectoryUserOut,
    summary="Sign in with email and password",
)
def login(
    payload: LoginRequest, db: DbSession, response: Response
) -> DirectoryUserOut:
    """Verify the password, then set the session and CSRF cookies.

    No CSRF token is required to reach this endpoint - there is no session to
    protect yet, and requiring one would make signing in impossible.
    """
    email = payload.email.strip().lower()

    user = db.scalar(
        select(User)
        .where(func.lower(User.email) == email)
        .options(selectinload(User.department))
    )

    # verify_password burns a hash even when there is no user, so a missing
    # account and a wrong password take similar time.
    if user is None or not verify_password(payload.password, user.password_hash):
        logger.warning("Failed sign-in attempt for %s", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=messages.BAD_CREDENTIALS,
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=messages.ACTOR_DEACTIVATED.format(email=user.email),
        )

    user.last_login_at = timeutil.now_utc()
    db.commit()

    response.set_cookie(SESSION_COOKIE, issue_session(user.id), **session_cookie_kwargs())
    response.set_cookie(CSRF_COOKIE, generate_csrf_token(), **csrf_cookie_kwargs())

    logger.info("Signed in %s (%s)", user.email, user.role.value)
    return _as_directory_user(user)


@router.post("/auth/logout", summary="Sign out")
def logout(request: Request, response: Response) -> dict[str, str]:
    """Clear both cookies.

    Still CSRF-checked: signing somebody out against their will is a real, if
    minor, attack, and the check costs nothing.
    """
    if not csrf_matches(
        request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=messages.CSRF_FAILED
        )

    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"detail": messages.SIGNED_OUT}


@router.get(
    "/me",
    response_model=DirectoryUserOut,
    summary="Who the caller is",
)
def whoami(actor: CurrentUser) -> DirectoryUserOut:
    """The signed-in user.

    The frontend calls this at boot; a 401 sends it to the login screen.
    """
    return _as_directory_user(actor)

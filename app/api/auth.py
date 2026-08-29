"""Sign in, sign out, and who am I.

Login is deliberately generous about time and stingy about information: a wrong
address and a wrong password produce the same message, so the form cannot be
used to discover who has an account.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core import messages
from app.core import time as timeutil
from app.core import google_oauth
from app.core.auth import CurrentUser
from app.core.google_oauth import GoogleAuthError
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

    logger.info("Signed in %s", user.email)
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


# =============================================================================
# Google Sign-In
# =============================================================================
# Complete on the server. The page shows no button yet; adding one is a link to
# /api/auth/google/start and nothing else.


def _find_or_create(db: Session, identity: google_oauth.GoogleIdentity) -> User:
    """Match the Google account to a directory entry, creating one if allowed.

    Matching is on email, which is safe here because the domain is verified and
    controlled by the company - nobody else can hold an @shreenm.com address.
    """
    user = db.scalar(
        select(User)
        .where(func.lower(User.email) == identity.email)
        .options(selectinload(User.department))
    )

    if user is not None:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=messages.ACTOR_DEACTIVATED.format(email=user.email),
            )
        # Google is authoritative for the display name.
        if identity.full_name and user.full_name != identity.full_name:
            user.full_name = identity.full_name
        return user

    if not settings.google_auto_create_users:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=messages.ACTOR_UNKNOWN.format(email=identity.email),
        )

    # First sign-in. No password is set: this account signs in with Google only,
    # and verify_password refuses a null hash outright.
    user = User(
        full_name=identity.full_name,
        email=identity.email,
        department_id=None,
        is_active=True,
        password_hash=None,
    )
    db.add(user)
    db.flush()
    logger.info("Created directory entry for %s from Google sign-in", user.email)
    return user


@router.get(
    "/auth/google/start",
    summary="Begin Google Sign-In",
    response_class=RedirectResponse,
)
def google_start(response: Response) -> RedirectResponse:
    """Redirect the browser to Google's consent screen.

    Sets a short-lived signed cookie holding the CSRF state and the PKCE
    verifier, both of which are checked when Google redirects back.
    """
    try:
        url, cookie = google_oauth.build_authorisation_url()
    except GoogleAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail
        ) from exc

    redirect = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    redirect.set_cookie(
        google_oauth.OAUTH_COOKIE, cookie, **google_oauth.oauth_cookie_kwargs()
    )
    return redirect


@router.get(
    "/auth/google/callback",
    summary="Google Sign-In callback",
    response_class=RedirectResponse,
)
def google_callback(
    request: Request,
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Complete the exchange and sign the user in.

    Ends in a redirect either way: to the grid on success, or back to the
    sign-in page carrying the reason, because a browser landing here has no
    JavaScript waiting to read a JSON body.
    """

    def back_to_login(detail: str) -> RedirectResponse:
        logger.warning("Google sign-in refused: %s", detail)
        target = f"/login.html?error={quote(detail)}"
        failed = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        failed.delete_cookie(google_oauth.OAUTH_COOKIE, path="/")
        return failed

    if error:
        # The user pressed Cancel on Google's screen, most likely.
        return back_to_login(messages.GOOGLE_FAILED)

    if not code or not state:
        return back_to_login(messages.GOOGLE_FAILED)

    try:
        expected_state, verifier = google_oauth.read_oauth_cookie(
            request.cookies.get(google_oauth.OAUTH_COOKIE)
        )
    except GoogleAuthError as exc:
        return back_to_login(exc.detail)

    # The state check is what stops a login-CSRF: an attacker cannot make a
    # victim's browser complete a sign-in the attacker started.
    if not hmac.compare_digest(state, expected_state):
        return back_to_login(messages.GOOGLE_STATE_MISMATCH)

    try:
        raw_token = google_oauth.exchange_code(code, verifier)
        identity = google_oauth.verify_id_token(raw_token)
    except GoogleAuthError as exc:
        return back_to_login(exc.detail)

    try:
        user = _find_or_create(db, identity)
    except HTTPException as exc:
        return back_to_login(str(exc.detail))

    user.last_login_at = timeutil.now_utc()
    db.commit()

    signed_in = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    signed_in.delete_cookie(google_oauth.OAUTH_COOKIE, path="/")
    signed_in.set_cookie(
        SESSION_COOKIE, issue_session(user.id), **session_cookie_kwargs()
    )
    signed_in.set_cookie(CSRF_COOKIE, generate_csrf_token(), **csrf_cookie_kwargs())

    logger.info("Signed in %s via Google", user.email)
    return signed_in


@router.get("/auth/google/status", summary="Is Google Sign-In configured?")
def google_status() -> dict[str, object]:
    """Lets the page decide whether to show the button, once it has one."""
    return {
        "configured": google_oauth.is_configured(),
        "domain": settings.allowed_email_domain,
        "start_url": "/api/auth/google/start",
    }

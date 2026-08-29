"""Password hashing, session cookies and CSRF tokens.

Sessions are stateless: the cookie carries the user id, signed with
``SECRET_KEY`` by itsdangerous and stamped with an age. Nothing is kept in
memory, which matters because Render runs a single worker that is stopped and
restarted freely - a server-side session store would log everyone out on every
cold start, and a dict would not survive one either.

The cookie is HttpOnly so script cannot read it, SameSite=Lax so it is not sent
on a cross-site POST, and Secure in production. Because SameSite=Lax is not a
complete defence on its own, state-changing requests also carry a CSRF token in
a header, checked against a second cookie - the double-submit pattern. That
second cookie is deliberately readable by script; it has to be, for the page to
echo it back.
"""

from __future__ import annotations

import hmac
import logging
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "nm_session"
CSRF_COOKIE = "nm_csrf"
CSRF_HEADER = "X-CSRF-Token"

_SALT = "nm-meet-session"

# bcrypt truncates at 72 bytes and raises on longer input in 4.x.
MAX_PASSWORD_BYTES = 72


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=_SALT)


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    """Hash a password with bcrypt, including its own per-password salt."""
    encoded = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password against a stored hash.

    A user with no password set always fails, and the comparison still runs a
    hash so that "no such account" and "wrong password" take similar time.
    """
    encoded = password.encode("utf-8")[:MAX_PASSWORD_BYTES]

    if not password_hash:
        # Burn roughly the same time as a real check, so absence is not timeable.
        bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=settings.bcrypt_rounds))
        return False

    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        logger.warning("Stored password hash is not valid bcrypt")
        return False


def generate_password(length: int = 12) -> str:
    """A readable random password for seeding.

    Ambiguous characters are left out so somebody can retype it from a terminal
    without wondering whether that is a 1 or an l.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ------------------------------------------------------------------- sessions


def issue_session(user_id: int) -> str:
    """Sign a session token for this user."""
    return _serializer().dumps({"uid": user_id})


def read_session(token: str) -> int | None:
    """Return the user id from a session token, or None if it is not usable."""
    try:
        payload = _serializer().loads(token, max_age=settings.session_max_age_seconds)
    except SignatureExpired:
        logger.info("Session token expired")
        return None
    except BadSignature:
        logger.warning("Session token failed signature check")
        return None

    uid = payload.get("uid") if isinstance(payload, dict) else None
    return uid if isinstance(uid, int) else None


# ----------------------------------------------------------------------- csrf


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: str | None, header_value: str | None) -> bool:
    """Double-submit check, compared in constant time."""
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


# ------------------------------------------------------------- cookie options


def session_cookie_kwargs() -> dict:
    """Cookie flags shared by every place that sets the session cookie."""
    return {
        "httponly": True,
        "secure": settings.is_production,
        "samesite": "lax",
        "max_age": settings.session_max_age_seconds,
        "path": "/",
    }


def csrf_cookie_kwargs() -> dict:
    """The CSRF cookie must be readable by script, so it is not HttpOnly."""
    return {
        "httponly": False,
        "secure": settings.is_production,
        "samesite": "lax",
        "max_age": settings.session_max_age_seconds,
        "path": "/",
    }

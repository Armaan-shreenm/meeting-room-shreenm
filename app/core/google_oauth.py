"""Google Sign-In - the OAuth 2.0 / OpenID Connect exchange.

Every employee has an ``@shreenm.com`` Google account, so this is intended to
become the only way into NM Meet. **The backend here is complete.** The page
deliberately shows no button yet; adding one is a link to
``/api/auth/google/start`` and nothing more.

The flow, and why each piece is there:

1. ``/api/auth/google/start`` mints a random ``state`` and a PKCE ``code_verifier``,
   stores both in a short-lived signed cookie, and redirects to Google.
2. The user authenticates with Google. Google redirects back with a ``code``.
3. ``/api/auth/google/callback`` checks ``state`` matches the cookie - that is
   what stops somebody feeding a victim a login link of their own choosing - 
   then exchanges the code, sending the ``code_verifier`` so an intercepted
   code is useless without it.
4. The returned ``id_token`` is verified **cryptographically** against Google's
   published keys: signature, issuer, audience and expiry. Never trusted because
   it arrived over HTTPS.
5. The email must be verified by Google and inside the allowed domain. A
   personal gmail account signed into the same browser is refused by name.
6. A directory entry is created if this is their first sign-in, and the same
   session cookie the password login issues is set. Everything downstream - 
   ``get_current_user``, permissions, CSRF - is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

logger = logging.getLogger(__name__)

AUTHORISE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Identity only. NM Meet reads no calendar and no contacts, so it asks for
# nothing that would make an employee hesitate at the consent screen.
SCOPES = ("openid", "email", "profile")

OAUTH_COOKIE = "nm_oauth"
_OAUTH_SALT = "nm-meet-google-oauth"
# The round trip through Google is a browser redirect; ten minutes is generous.
OAUTH_STATE_MAX_AGE = 600

# How far this machine's clock may disagree with Google's before an id_token is
# refused. Verifying with no tolerance at all sounds stricter and is really just
# brittle: an ordinary desktop drifts a second or two between NTP syncs, and a
# clock one second slow makes every single sign-in fail with "Token used too
# early" - a message the user cannot act on, for a machine that is working
# normally. Sixty seconds is what OpenID Connect suggests and what other
# libraries default to. It buys an attacker nothing: the signature, the issuer,
# the audience and the expiry are all still checked, and a token is only usable
# a minute either side of a window it already had to be inside.
CLOCK_SKEW_SECONDS = 60


class GoogleAuthError(Exception):
    """The exchange failed. ``detail`` is already a user-facing message."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class GoogleIdentity:
    """What Google told us, after verification."""

    email: str
    full_name: str
    google_sub: str
    picture: str | None = None


def is_configured() -> bool:
    """Whether a client id and secret have been supplied."""
    return bool(settings.google_client_id and settings.google_client_secret)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=_OAUTH_SALT)


# ------------------------------------------------------------------- PKCE


def _code_verifier() -> str:
    """A high-entropy secret this server keeps until the exchange."""
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    """S256 challenge: Google only ever sees the hash."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ---------------------------------------------------------------- step one


def build_authorisation_url() -> tuple[str, str]:
    """Return (url to send the browser to, value for the oauth cookie)."""
    if not is_configured():
        from app.core import messages

        raise GoogleAuthError(messages.GOOGLE_NOT_CONFIGURED)

    state = secrets.token_urlsafe(32)
    verifier = _code_verifier()

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        # Restricts the account chooser to the work domain. A hint, not a
        # control - the domain is enforced again on the verified token below.
        "hd": settings.allowed_email_domain,
        "prompt": "select_account",
    }

    cookie = _serializer().dumps({"state": state, "verifier": verifier})
    return f"{AUTHORISE_URL}?{urlencode(params)}", cookie


# ---------------------------------------------------------------- step two


def read_oauth_cookie(raw: str | None) -> tuple[str, str]:
    """Recover (state, verifier) from the cookie, or refuse."""
    from app.core import messages

    if not raw:
        raise GoogleAuthError(messages.GOOGLE_STATE_MISMATCH)

    try:
        payload = _serializer().loads(raw, max_age=OAUTH_STATE_MAX_AGE)
    except SignatureExpired as exc:
        raise GoogleAuthError(messages.GOOGLE_STATE_MISMATCH) from exc
    except BadSignature as exc:
        logger.warning("OAuth cookie failed its signature check")
        raise GoogleAuthError(messages.GOOGLE_STATE_MISMATCH) from exc

    state = payload.get("state") if isinstance(payload, dict) else None
    verifier = payload.get("verifier") if isinstance(payload, dict) else None
    if not state or not verifier:
        raise GoogleAuthError(messages.GOOGLE_STATE_MISMATCH)

    return state, verifier


def exchange_code(code: str, verifier: str) -> str:
    """Swap the authorisation code for an id_token. Returns the raw JWT."""
    from app.core import messages

    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }

    try:
        response = httpx.post(TOKEN_URL, data=data, timeout=15)
    except httpx.HTTPError as exc:
        logger.error("Could not reach Google's token endpoint: %s", exc)
        raise GoogleAuthError(messages.GOOGLE_FAILED) from exc

    if response.status_code != 200:
        # Google's body names the fault (bad secret, reused code, mismatched
        # redirect). It goes to the log; the user gets one sentence.
        logger.error(
            "Google token exchange failed: %s %s", response.status_code, response.text
        )
        raise GoogleAuthError(messages.GOOGLE_FAILED)

    id_token = response.json().get("id_token")
    if not id_token:
        logger.error("Google returned no id_token")
        raise GoogleAuthError(messages.GOOGLE_FAILED)

    return id_token


def verify_id_token(raw_id_token: str) -> GoogleIdentity:
    """Verify signature, issuer, audience and expiry against Google's keys.

    Arriving over HTTPS is not evidence of anything. This is the check that
    makes the identity trustworthy.
    """
    from app.core import messages

    try:
        claims = google_id_token.verify_oauth2_token(
            raw_id_token,
            google_requests.Request(),
            settings.google_client_id,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )
    except ValueError as exc:
        logger.warning("Google id_token failed verification: %s", exc)
        raise GoogleAuthError(messages.GOOGLE_FAILED) from exc

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError(messages.GOOGLE_FAILED)

    if not claims.get("email_verified", False):
        raise GoogleAuthError(messages.GOOGLE_EMAIL_UNVERIFIED.format(email=email))

    domain = settings.allowed_email_domain.lower()
    if not email.endswith(f"@{domain}"):
        logger.warning("Refused Google sign-in for out-of-domain address %s", email)
        raise GoogleAuthError(
            messages.GOOGLE_WRONG_DOMAIN.format(email=email, domain=domain)
        )

    name = (claims.get("name") or "").strip()
    if not name:
        # Fall back to the local part: "priya.nair" becomes "Priya Nair".
        name = email.split("@", 1)[0].replace(".", " ").replace("_", " ").title()

    return GoogleIdentity(
        email=email,
        full_name=name,
        google_sub=str(claims.get("sub", "")),
        picture=claims.get("picture"),
    )


def oauth_cookie_kwargs() -> dict:
    """Flags for the short-lived state cookie."""
    return {
        "httponly": True,
        "secure": settings.is_production,
        # Google's redirect back is a cross-site top-level GET, which "lax"
        # allows and "strict" would silently drop.
        "samesite": "lax",
        "max_age": OAUTH_STATE_MAX_AGE,
        "path": "/",
    }

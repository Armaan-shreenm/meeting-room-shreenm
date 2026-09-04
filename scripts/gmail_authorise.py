"""Mint the Gmail refresh token, once, on somebody's laptop.

Run this exactly once, signed in as the mailbox the notifications should come
from. It opens a browser, waits for the one "Allow", and prints a refresh token
to paste into the environment. Nothing it produces belongs in this repository.

    python -m scripts.gmail_authorise

The mailbox is very often signed in on somebody else's machine, and the redirect
then lands on a localhost that is not listening there. Nothing is lost when that
happens: the authorisation code is sitting in that browser's address bar. Copy
the whole failed URL over and hand it in:

    python -m scripts.gmail_authorise --url "http://localhost:8765/oauth2callback?code=..."

Why a script and not a page in the app: the consent has to happen in a browser
signed in as that mailbox, and it happens once in the life of the deployment.
Building it into the running service would mean an endpoint that exists to be
used a single time and is a liability every day after.

The redirect lands on localhost because that is where the browser is. Render
never performs this exchange - it only ever holds the refresh token and trades
it for an hour-long access token, which is what makes this work from a host
that blocks SMTP.

The scope is ``gmail.send`` alone. The resulting token can put a message in the
outbox and cannot read one.
"""

from __future__ import annotations

import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

from app.config import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.send"

# Must match the Authorized redirect URI registered on the OAuth client exactly.
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/oauth2callback"

_result: dict[str, str] = {}


class _Callback(http.server.BaseHTTPRequestHandler):
    """Catches the one redirect Google makes, then the server stops."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's own spelling
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth2callback":
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query)
        _result["code"] = (params.get("code") or [""])[0]
        _result["state"] = (params.get("state") or [""])[0]
        _result["error"] = (params.get("error") or [""])[0]

        body = (
            b"<h2>Done. You can close this tab.</h2>"
            if _result["code"]
            else b"<h2>Refused. Nothing was changed.</h2>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Quiet: the console is carrying the instructions."""


def _exchange(code: str) -> int:
    """Trade one authorisation code for a refresh token, and print it."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        print(f"Token exchange failed: {response.status_code} {response.text}")
        print()
        print("An authorisation code is single use and lasts about ten minutes.")
        print("Press Allow again and bring the fresh URL over.")
        return 1

    refresh = response.json().get("refresh_token")
    if not refresh:
        print(
            "Google returned no refresh token. That happens when this account "
            "has already granted the client and Google saw no need to issue a "
            "second one - revoke it at https://myaccount.google.com/permissions "
            "and try again."
        )
        return 1

    print()
    print("=" * 70)
    print("GMAIL_REFRESH_TOKEN=" + refresh)
    print("=" * 70)
    return 0


def main() -> int:
    if not settings.gmail_client_id or not settings.gmail_client_secret:
        print("GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env first.")
        return 1

    # Handed a URL copied off another machine: the browser there could not reach
    # this listener, but the code it was given is in the address bar all the same.
    if "--url" in sys.argv:
        raw = sys.argv[sys.argv.index("--url") + 1]
        query = urllib.parse.urlparse(raw).query
        code = (urllib.parse.parse_qs(query).get("code") or [""])[0]
        if not code:
            print(
                "No ?code= in that URL. Copy the whole address bar, including "
                "everything after the question mark."
            )
            return 1
        return _exchange(code)

    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.gmail_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent together are what actually return a refresh token.
        # Without them Google hands back an access token that dies in an hour
        # and this whole exercise has to be repeated every hour.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("127.0.0.1", PORT), _Callback)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print()
    print("=" * 70)
    print("Sign in as the mailbox the notifications should be sent FROM.")
    print("If the browser does not open, paste this into it:")
    print()
    print(url)
    print("=" * 70)
    print()
    webbrowser.open(url)

    for _ in range(1800):  # fifteen minutes; nobody should have to hurry
        if _result:
            break
        threading.Event().wait(0.5)
    server.server_close()

    if not _result:
        print("Timed out waiting for the browser.")
        return 1
    if _result.get("error"):
        print(f"Google refused: {_result['error']}")
        return 1
    if _result.get("state") != state:
        print("State mismatch - refusing the response.")
        return 1

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": _result["code"],
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        print(f"Token exchange failed: {response.status_code} {response.text}")
        return 1

    payload = response.json()
    refresh = payload.get("refresh_token")
    if not refresh:
        print(
            "Google returned no refresh token. That happens when this account "
            "has already granted the client and Google saw no need to issue a "
            "second one - revoke it at https://myaccount.google.com/permissions "
            "and run this again."
        )
        return 1

    print()
    print("=" * 70)
    print("GMAIL_REFRESH_TOKEN=" + refresh)
    print("=" * 70)
    print()
    print("Put that in .env and in the Render dashboard. It does not expire,")
    print("unless the OAuth consent screen is still in Testing - those tokens")
    print("die after seven days, so the screen must be Internal or Published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

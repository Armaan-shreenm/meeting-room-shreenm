"""Notification transports.

The interface is :class:`app.services.notifications.Transport`: one ``send``
method that either returns or raises. Raising marks the ``notification_log`` row
FAILED with the reason; returning marks it SENT.

Two implementations:

* :class:`StdoutTransport` (in ``notifications``) renders the whole message to
  stdout. That is what runs when SMTP is not configured, and it is not a
  degraded mode - on Render the log stream is a real, greppable record.
* :class:`SmtpTransport` here sends real mail over SMTP.
* :class:`GmailApiTransport` sends it over HTTPS instead, which is the only
  thing that works where SMTP ports are blocked. Preferred when a refresh
  token is configured.

Selection is by configuration alone, in :func:`configure_transport`. Nothing else
in the system knows or cares which one is installed.
"""

from __future__ import annotations

import base64
import logging
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx

from app.config import settings
from app.services.notifications import (
    RenderedMessage,
    StdoutTransport,
    set_transport,
)

logger = logging.getLogger(__name__)


def _ipv4_socket(host: str, port: int, timeout: float):
    """Open a TCP connection to ``host``, over IPv4 only.

    ``socket.create_connection`` walks whatever ``getaddrinfo`` returns. Where
    that includes an IPv6 address and the host has no IPv6 route - which is the
    case on Render - the attempt fails with ``[Errno 101] Network is
    unreachable`` and the mail never leaves. Asking for AF_INET makes the
    resolver return only addresses this container can actually reach.

    Falls back to the ordinary dual-stack connect if the name has no IPv4
    address at all, so this cannot make a working setup stop working.
    """
    try:
        candidates = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        candidates = []

    if not candidates:
        return socket.create_connection((host, port), timeout)

    last: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in candidates:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()

    raise last if last is not None else OSError(f"could not reach {host}:{port}")


class _IPv4SMTP(smtplib.SMTP):
    """smtplib, but never over IPv6.

    Only the socket changes. ``self._host`` is still the hostname, so STARTTLS
    validates the certificate against the name and not against an address.
    """

    def _get_socket(self, host, port, timeout):  # noqa: D102 - overriding smtplib
        return _ipv4_socket(host, port, timeout)


class SmtpTransport:
    """Send a rendered message over SMTP.

    Connects per message and closes. A booking produces at most five messages,
    so a pooled connection would buy little and would have to survive a worker
    that Render stops without warning.
    """

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        use_starttls: bool = True,
        from_email: str = "",
        from_name: str = "",
        timeout: int = 20,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_starttls = use_starttls
        self.from_email = from_email
        self.from_name = from_name
        self.timeout = timeout

    def build(self, message: RenderedMessage) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = message.subject
        mail["From"] = formataddr((self.from_name, self.from_email))
        mail["To"] = formataddr((message.recipient.name, message.recipient.email))
        mail["Message-ID"] = make_msgid(domain="nm-meet")
        mail.set_content(message.body)
        return mail

    def send(self, message: RenderedMessage) -> None:
        mail = self.build(message)

        with _IPv4SMTP(self.host, self.port, timeout=self.timeout) as server:
            server.ehlo()
            if self.use_starttls:
                server.starttls()
                server.ehlo()
            if self.username:
                server.login(self.username, self.password)
            server.send_message(mail)

        logger.info(
            "Sent %s to %s over SMTP", message.event.value, message.recipient.email
        )


class GmailApiTransport:
    """Send through Gmail's REST API, over HTTPS.

    Exists because SMTP is not reachable everywhere. Render's free instances
    block outbound 25, 465 and 587 outright - the connection is not refused,
    it is dropped, so every message dies on a twenty-second timeout - while
    HTTPS is wide open. This talks to Gmail on 443 like any other API call.

    Authentication is a refresh token for one mailbox, minted once by
    scripts/gmail_authorise.py. The scope is ``gmail.send`` and nothing else:
    this may put a message in the outbox and may not read a single one. An
    app password, by contrast, is full IMAP and SMTP access to the account.
    """

    name = "gmail-api"

    SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        from_email: str = "",
        from_name: str = "",
        timeout: int = 20,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.from_email = from_email
        self.from_name = from_name
        self.timeout = timeout

    def build(self, message: RenderedMessage) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = message.subject
        mail["From"] = formataddr((self.from_name, self.from_email))
        mail["To"] = formataddr((message.recipient.name, message.recipient.email))
        mail.set_content(message.body)
        return mail

    def _access_token(self) -> str:
        """Trade the refresh token for an access token good for an hour.

        Done per message rather than cached: a booking sends two or three, the
        call costs a fraction of a second, and a cache would have to survive a
        worker Render stops without warning.
        """
        response = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            # Google's body names the fault - a revoked token, a wrong secret,
            # a consent screen still in testing whose tokens expire after seven
            # days. It goes to the log; the caller only needs to know it failed.
            raise RuntimeError(
                f"Gmail refused the refresh token: {response.status_code} {response.text}"
            )

        token = response.json().get("access_token")
        if not token:
            raise RuntimeError("Gmail returned no access token")
        return token

    def send(self, message: RenderedMessage) -> None:
        mail = self.build(message)
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode("ascii")

        response = httpx.post(
            self.SEND_URL,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            json={"raw": raw},
            timeout=self.timeout,
        )
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"Gmail refused the message: {response.status_code} {response.text}"
            )

        logger.info(
            "Sent %s to %s over the Gmail API",
            message.event.value,
            message.recipient.email,
        )


def configure_transport() -> str:
    """Install the right transport for this environment, and say which.

    SMTP is used only when it is switched on *and* a host is configured. A
    half-configured relay falls back to stdout with a warning rather than
    failing every notification: section 8 says notifications are always sent, so
    losing them silently is the one outcome to avoid.
    """
    if settings.notifications_enabled and settings.gmail_refresh_token:
        set_transport(
            GmailApiTransport(
                client_id=settings.gmail_client_id,
                client_secret=settings.gmail_client_secret,
                refresh_token=settings.gmail_refresh_token,
                from_email=settings.smtp_from_email,
                from_name=settings.smtp_from_name,
            )
        )
        logger.info("Notifications will be sent over the Gmail API as %s", settings.smtp_from_email)
        return "gmail-api"

    if settings.notifications_enabled and settings.smtp_host:
        set_transport(
            SmtpTransport(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user,
                password=settings.smtp_password,
                use_starttls=settings.smtp_starttls,
                from_email=settings.smtp_from_email,
                from_name=settings.smtp_from_name,
            )
        )
        logger.info(
            "Notifications will be sent over SMTP via %s:%s",
            settings.smtp_host,
            settings.smtp_port,
        )
        return "smtp"

    if settings.notifications_enabled and not settings.smtp_host:
        logger.warning(
            "NOTIFICATIONS_ENABLED is true but SMTP_HOST is empty. "
            "Falling back to stdout so notifications are still recorded."
        )

    set_transport(StdoutTransport())
    logger.info("Notifications will be written to stdout")
    return "stdout"

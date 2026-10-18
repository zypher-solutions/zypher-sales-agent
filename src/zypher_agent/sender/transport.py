"""Getting one email onto the wire. The only module that talks to a mail server for sending.

`EmailTransport` is the interface; `SMTPTransport` is the real one, `FakeTransport` serves tests and --dry-run.
Errors are sorted into the only categories the worker needs to act on:

  TransientSendError  the server said "try later" (4xx) or was unreachable BEFORE it took the message: safe to retry
  PermanentSendError  the server refused this recipient (5xx): never retry, treat as a bounce
  AmbiguousSendError  the connection died AFTER we started sending the message: it may or may not have left.
                      Never retried automatically (a duplicate cold email is worse than a missed one): a human checks.
  SendConfigError     our login or setup is wrong: stop everything and tell the owner
"""
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from typing import Protocol

from zypher_agent.config import Settings


class SendError(Exception):
    pass


class TransientSendError(SendError):
    pass


class PermanentSendError(SendError):
    pass


class AmbiguousSendError(SendError):
    pass


class SendConfigError(SendError):
    pass


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    body: str
    message_id: str
    from_addr: str
    from_name: str = ""
    in_reply_to: str | None = None
    references: str | None = None
    html: str | None = None  # a styled copy of the SAME text; the plain part above is always sent too


class EmailTransport(Protocol):
    def send(self, email: OutgoingEmail) -> None: ...


def new_message_id(from_addr: str) -> str:
    """A unique Message-ID on the sender's own domain, e.g. <2026...@zypher-solutions.com>."""
    return make_msgid(domain=from_addr.split("@")[-1])


def build_mime(email: OutgoingEmail, now: datetime | None = None) -> EmailMessage:
    """Plain text always; an optional styled HTML copy of the same words. Never images, never a tracking pixel."""
    msg = EmailMessage()
    msg["From"] = formataddr((email.from_name, email.from_addr)) if email.from_name else email.from_addr
    msg["To"] = email.to
    msg["Reply-To"] = email.from_addr
    msg["Subject"] = email.subject
    msg["Date"] = format_datetime(now or datetime.now(UTC))
    msg["Message-ID"] = email.message_id
    if email.in_reply_to:
        msg["In-Reply-To"] = email.in_reply_to
        msg["References"] = email.references or email.in_reply_to
    # One-click opt-out without a website: a reply to this address with the word "unsubscribe".
    msg["List-Unsubscribe"] = f"<mailto:{email.from_addr}?subject=unsubscribe>"
    msg.set_content(email.body, charset="utf-8")
    if email.html:
        msg.add_alternative(email.html, subtype="html")
    return msg


class SMTPTransport:
    def __init__(self, settings: Settings, timeout: float = 30.0) -> None:
        if settings.mail_password is None:
            raise SendConfigError("MAIL_PASSWORD is not set in .env")
        self.host, self.port, self.timeout = settings.smtp_host, settings.smtp_port, timeout
        self.user = settings.mail_user or settings.sender_email
        self._password = settings.mail_password.get_secret_value()

    def __repr__(self) -> str:
        return f"SMTPTransport({self.host}:{self.port}, user={self.user}, password=***)"

    def _connect(self) -> smtplib.SMTP:
        ctx = ssl.create_default_context()
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            server.starttls(context=ctx)
        server.login(self.user, self._password)
        return server

    def check_login(self) -> None:
        """Connect and log in, send nothing. Used by `preflight`."""
        try:
            self._connect().quit()
        except smtplib.SMTPAuthenticationError as e:
            raise SendConfigError(f"SMTP login refused ({e.smtp_code}): check MAIL_USER and MAIL_PASSWORD") from None
        except (smtplib.SMTPException, OSError) as e:
            raise TransientSendError(f"could not reach {self.host}:{self.port} ({type(e).__name__})") from None

    def send(self, email: OutgoingEmail) -> None:
        # Phase 1 of the call (connect + login) fails safely; phase 2 (the message itself) may be ambiguous.
        try:
            server = self._connect()
        except smtplib.SMTPAuthenticationError as e:
            raise SendConfigError(f"SMTP login refused ({e.smtp_code})") from None
        except (smtplib.SMTPException, OSError) as e:
            raise TransientSendError(f"could not connect ({type(e).__name__})") from None
        try:
            server.send_message(build_mime(email))
        except smtplib.SMTPRecipientsRefused as e:
            raise PermanentSendError(_refusal(e)) from None
        except smtplib.SMTPResponseException as e:
            text = f"{e.smtp_code} {_clean(e.smtp_error)}"
            raise (PermanentSendError if 500 <= e.smtp_code < 600 else TransientSendError)(text) from None
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as e:
            raise AmbiguousSendError(f"connection lost while sending ({type(e).__name__}); check the Sent folder") from None
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - closing a dead connection must not hide the real result
                pass


def _clean(raw: object) -> str:
    return (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))[:200]


def _refusal(e: smtplib.SMTPRecipientsRefused) -> str:
    return "; ".join(f"{addr}: {code} {_clean(msg)}" for addr, (code, msg) in e.recipients.items())


class FakeTransport:
    """Records instead of sending. `fail_with` maps a recipient address to an exception to raise."""

    def __init__(self, fail_with: dict[str, Exception] | None = None) -> None:
        self.sent: list[OutgoingEmail] = []
        self.fail_with = fail_with or {}

    def send(self, email: OutgoingEmail) -> None:
        if email.to in self.fail_with:
            raise self.fail_with[email.to]
        self.sent.append(email)

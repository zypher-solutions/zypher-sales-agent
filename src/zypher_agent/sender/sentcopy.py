"""Put a copy of each sent email into the mailbox's Sent folder.

Sending straight through the mail server delivers the email, but the server does not file a copy anywhere:
only mail apps do that, by adding a copy to Sent themselves. Since this program sends without a mail app,
we do the same step, so every email we send is visible in your Titan or webmail "Sent" list.

This is a courtesy and never part of the send: it runs only AFTER a successful send, and a failure here is
logged and cannot change the result, retry the email, or stop the worker.
"""
import imaplib
import ssl
import time

from zypher_agent.config import Settings


class SentCopyError(Exception):
    pass


class IMAPSentSaver:
    """Adds a message to the Sent folder over IMAP, already marked as read, with its real send time."""

    def __init__(self, settings: Settings, timeout: float = 30.0) -> None:
        if settings.mail_password is None:
            raise SentCopyError("MAIL_PASSWORD is not set")
        self.s, self.timeout = settings, timeout

    def __repr__(self) -> str:
        return f"IMAPSentSaver({self.s.imap_host}, folder={self.s.sent_folder!r}, password=***)"

    def __call__(self, raw: bytes) -> None:
        box = None
        try:
            box = imaplib.IMAP4_SSL(
                self.s.imap_host, self.s.imap_port, ssl_context=ssl.create_default_context(), timeout=self.timeout
            )
            box.login(self.s.mail_user or self.s.sender_email, self.s.mail_password.get_secret_value())
            folder = self.s.sent_folder
            status, detail = box.append(f'"{folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), raw)
            if status != "OK":
                raise SentCopyError(f"the server refused the copy: {detail}")
        except (imaplib.IMAP4.error, OSError) as e:
            raise SentCopyError(f"{type(e).__name__}: could not save to {self.s.sent_folder!r}") from None
        finally:
            if box is not None:
                try:
                    box.logout()
                except Exception:  # noqa: BLE001 - closing a dead connection must not hide the real result
                    pass

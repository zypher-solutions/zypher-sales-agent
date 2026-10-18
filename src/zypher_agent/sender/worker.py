"""The send worker: one pass over approved emails, each judged by the send policy.

Safety design (read this before changing anything):
  * Every message must pass `decide()` at the moment of sending, not just when it was approved.
  * A message is CLAIMED (approved -> queued) in one atomic database update before anything is sent.
    A crash or a second worker can therefore never send the same email twice.
  * Network failures are sorted by what we know. "Server said try later" is retried; "server refused this
    address" becomes a bounce; "connection died mid-send" is never retried automatically, because a duplicate
    cold email is worse than a missed one.
  * The master switch (`sending_enabled`) and the optional recipient allowlist are checked first.
"""
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from zypher_agent.config import Settings
from zypher_agent.db import Repository
from zypher_agent.guardrails.checks import check_draft
from zypher_agent.models import EmailDraft, Footer, Hypothesis, Lead, Message
from zypher_agent.sender.policy import SendContext, decide
from zypher_agent.sender.transport import (
    AmbiguousSendError,
    EmailTransport,
    OutgoingEmail,
    PermanentSendError,
    SendConfigError,
    TransientSendError,
    new_message_id,
)

MAX_ATTEMPTS = 3
RETRY_MINUTES = 15
MIN_GAP_SECONDS = (240, 720)  # random spacing between sends: 4 to 12 minutes


@dataclass
class SendSummary:
    sent: int = 0
    deferred: int = 0
    blocked: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    paused: str | None = None
    disabled: bool = False


def allowlisted(email: str, allowlist: str) -> bool:
    """True if no allowlist is set, or the address (or its domain) is on it."""
    entries = [e.strip().lower() for e in allowlist.split(",") if e.strip()]
    email = email.lower()
    return not entries or any(email == e or email.endswith("@" + e.lstrip("@")) for e in entries)


class SendWorker:
    def __init__(
        self, repo: Repository, transport: EmailTransport, settings: Settings, footer: Footer, icp,
        alert: Callable[[str], None] = lambda text: None, rng: random.Random | None = None,
    ) -> None:
        self.repo, self.transport, self.settings, self.footer, self.icp = repo, transport, settings, footer, icp
        self.alert, self.rng = alert, rng or random.Random()
        self.sender_tz = ZoneInfo(settings.sender_timezone)

    # ---------- one pass ----------

    def run_once(self, now: datetime | None = None) -> SendSummary:
        now = now or datetime.now(UTC)
        summary = SendSummary()
        if not self.settings.sending_enabled:
            summary.disabled = True
            return summary
        paused, reason = self.repo.sending_state()
        if paused:
            summary.paused = reason or "paused"
            return summary
        for msg in self.repo.approved_to_send(now):
            try:
                self._handle(msg, now, summary)
            except SendConfigError as e:  # our login is wrong: nothing will work, so stop and say so
                self._pause(f"mailbox login failed: {e}")
                summary.paused = str(e)
                return summary
        return summary

    # ---------- one message ----------

    def _handle(self, msg: Message, now: datetime, summary: SendSummary) -> None:
        lead = self.repo.get_lead(msg.contact_id)
        if not lead.email:
            return self._block(msg, lead, "no email address", summary)
        if not allowlisted(lead.email, self.settings.send_allowlist):
            return self._block(msg, lead, "not on the test allowlist (SEND_ALLOWLIST)", summary, quiet=True)

        stats = self.repo.send_stats(self._day_start(now))
        decision = decide(SendContext(
            now=now, country=lead.country, status=msg.status, version=msg.version,
            approved_version=self.repo.approved_version(msg.id), paused=False,
            suppressed=self.repo.is_suppressed(lead.email, lead.domain), violations=self._violations(msg),
            step=msg.step, company_already_contacted=self.repo.company_contacted(lead.company_id, lead.contact_id),
            sent_today=stats.sent_today, first_sent_at=stats.first_sent_at, last_sent_at=stats.last_sent_at,
            min_gap_seconds=self.rng.randint(*MIN_GAP_SECONDS), env_cap=self.settings.daily_send_cap,
            sender_tz=self.sender_tz,
        ))
        if decision.kind == "block":
            return self._block(msg, lead, decision.reason, summary)
        if decision.kind == "defer":
            self.repo.update_message(msg.id, send_after=decision.until)
            summary.deferred += 1
            return None
        self._send(msg, lead, now, summary)
        return None

    def _violations(self, msg: Message) -> list[str]:
        """Re-run today's guardrails on the stored draft: a rule tightened after approval still applies."""
        ev = msg.evidence
        check = check_draft(EmailDraft(**ev["draft"]), [Hypothesis(**h) for h in ev["hypotheses"]], self.icp, self.footer)
        return [v.rule for v in check.violations]

    def _send(self, msg: Message, lead: Lead, now: datetime, summary: SendSummary) -> None:
        from_addr = self.footer.sender_email
        header = new_message_id(from_addr)
        if not self.repo.claim_for_sending(msg.id, header, from_addr):
            return  # another worker got it first
        subject, in_reply_to = msg.subject, None
        if msg.step > 1:
            first = self.repo.first_message_header(lead.contact_id)
            in_reply_to = first[0] if first else None
            subject = first[1] if first and first[1].lower().startswith("re:") else f"Re: {first[1] if first else msg.subject}"
        out = OutgoingEmail(
            to=lead.email, subject=subject, body=msg.body, message_id=header, from_addr=from_addr,
            from_name=self.footer.sender_name, in_reply_to=in_reply_to,
        )
        try:
            self.transport.send(out)
        except SendConfigError as e:
            # Nothing was sent. Give the message back so it goes out after the owner fixes the login and resumes.
            self.repo.update_message(msg.id, status="approved", last_error=str(e))
            raise
        except TransientSendError as e:
            attempts = msg.send_attempts + 1
            if attempts >= MAX_ATTEMPTS:
                self._fail(msg, lead, f"gave up after {attempts} attempts: {e}", summary)
            else:
                self.repo.update_message(
                    msg.id, status="approved", last_error=str(e), send_after=now + timedelta(minutes=RETRY_MINUTES * attempts)
                )
                summary.deferred += 1
            return
        except PermanentSendError as e:
            self.repo.update_message(msg.id, status="bounced", last_error=str(e))
            self.repo.add_suppression("bounce", email=lead.email)
            self.repo.set_lead_status(lead.contact_id, "bounced")
            self.repo.log_event("bounced", contact_id=lead.contact_id, message_id=msg.id, payload={"error": str(e), "at": "send"})
            summary.problems.append(f"{lead.company_name}: refused by the server ({e})")
            self.alert(f"Bounced: {lead.email} refused by the mail server ({e}). Added to the do-not-contact list.")
            return
        except AmbiguousSendError as e:
            self._fail(msg, lead, str(e), summary, ambiguous=True)
            return
        sent_at = datetime.now(UTC) if now.tzinfo is None else now
        self.repo.update_message(msg.id, status="sent", sent_at=sent_at, in_reply_to=in_reply_to, last_error=None)
        self.repo.set_lead_status(lead.contact_id, "in_sequence")
        self.repo.log_event("sent", contact_id=lead.contact_id, message_id=msg.id, actor="sender",
                            payload={"to": lead.email, "step": msg.step, "message_id": header})
        summary.sent += 1

    # ---------- outcomes ----------

    def _block(self, msg: Message, lead: Lead, reason: str, summary: SendSummary, quiet: bool = False) -> None:
        if "do-not-contact" in reason:  # opted out after approval: this email must never go
            self.repo.update_message(msg.id, status="rejected", last_error=reason)
            self.repo.set_lead_status(lead.contact_id, "unsubscribed")
        elif msg.last_error != reason:  # record once, so a stuck message does not flood the log
            self.repo.update_message(msg.id, last_error=reason)
            if not quiet:
                self.repo.log_event("send_blocked", contact_id=lead.contact_id, message_id=msg.id, actor="sender", payload={"reason": reason})
        if not quiet:
            summary.blocked.append(f"{lead.company_name}: {reason}")

    def _fail(self, msg: Message, lead: Lead, error: str, summary: SendSummary, ambiguous: bool = False) -> None:
        self.repo.update_message(msg.id, status="failed", last_error=error)
        self.repo.log_event("send_failed", contact_id=lead.contact_id, message_id=msg.id, actor="sender", payload={"error": error})
        summary.problems.append(f"{lead.company_name}: {error}")
        extra = " It may or may not have been delivered: check the Sent folder before doing anything." if ambiguous else ""
        self.alert(f"Send failed for {lead.email}: {error}.{extra} It will NOT be retried automatically.")

    def _pause(self, reason: str) -> None:
        self.repo.set_sending_paused(True, reason)
        self.repo.log_event("sending_paused", actor="sender", payload={"reason": reason})
        self.alert(f"Sending is PAUSED: {reason}. Fix it, then send /resume in this chat.")

    def _day_start(self, now: datetime) -> datetime:
        local = now.astimezone(self.sender_tz)
        return datetime.combine(local.date(), time(0, 0), tzinfo=self.sender_tz).astimezone(UTC)

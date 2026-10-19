"""Human approval over a chat channel (Telegram in production, the console in --dry-run).

This module is pure logic: it talks to a `Notifier`, never to Telegram directly, so every
rule below is unit-tested without a network. Approving a draft only records the decision.
Nothing here (or anywhere in Phase 1) can send an email.

Rules:
  * Only user ids on the allowlist can do anything. Everyone else is ignored and logged.
  * A button carries the draft's id AND version. Tapping an old card after a redraft is refused,
    so you can never approve text you have not seen.
  * Approve is hidden on a draft that failed the guardrails, and re-checked when pressed.
"""
import threading
import uuid
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel

from zypher_agent.config import Icp
from zypher_agent.db import Repository
from zypher_agent.drafting.email import evidence_for, generate
from zypher_agent.guardrails.checks import check_for_step
from zypher_agent.leads import clean_email, normalize_domain
from zypher_agent.llm import LLM, LLMError
from zypher_agent.models import EmailDraft, Footer, Hypothesis, Lead, Message

CARD_LIMIT = 3900  # Telegram's hard limit is 4096 characters per message
HELP = (
    "Tap Suggest changes under a draft, then type what to change. Approve and Reject are buttons.\n"
    "/suppress name@company.com  never contact this address again\n"
    "/suppress company.com  never contact anyone at this domain again\n"
    "/pause  stop all sending now    /resume  start again    /status  what is waiting and sent\n"
    "/queue  how many cards are waiting for you and how many are queued behind them\n"
    "/stats  emails sent, replies, bounces and opt-outs (last 7 days and all time)"
)


EMAIL_BANNER = "━━━ THE EMAIL (everything between the lines is what they receive) ━━━"
END_BANNER = "━━━ END OF EMAIL ━━━"
NOTES_BANNER = "📝 ONLY FOR YOU, NOT SENT: why the draft says what it says"
FOLLOWUP_BANNER = "━━━ EMAIL (same signature and opt-out line as email 1) ━━━"


class Button(BaseModel):
    text: str
    data: str


class Notifier(Protocol):
    def send(self, chat_id: int, text: str, buttons: list[Button] | None = None) -> int: ...
    def edit(self, chat_id: int, message_id: int, text: str) -> None: ...  # replaces text, removes buttons
    def set_buttons(self, chat_id: int, message_id: int, buttons: list[Button]) -> None: ...  # keeps the text
    def answer(self, callback_id: str, text: str) -> None: ...


class ConsoleNotifier:
    """Prints instead of sending. Records everything so --dry-run and tests can inspect it."""

    def __init__(self, echo: bool = True) -> None:
        self.echo, self.sent, self.edits, self.answers = echo, [], [], []

    def _say(self, label: str, text: str) -> None:
        if self.echo:
            print(f"\n[{label}]\n{text}")

    def send(self, chat_id: int, text: str, buttons: list[Button] | None = None) -> int:
        message_id = len(self.sent) + 1
        self.sent.append({"id": message_id, "chat_id": chat_id, "text": text, "buttons": buttons or []})
        shown = "   ".join(f"[{b.text}]" for b in buttons or [])
        self._say(f"TELEGRAM → {chat_id}", text + (f"\n\n{shown}" if shown else ""))
        return message_id

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append({"chat_id": chat_id, "id": message_id, "text": text})
        self._say(f"TELEGRAM edit #{message_id}", text.splitlines()[0])

    def set_buttons(self, chat_id: int, message_id: int, buttons: list[Button]) -> None:
        for card in self.sent:
            if card["id"] == message_id and card["chat_id"] == chat_id:
                card["buttons"] = buttons
        self._say(f"TELEGRAM buttons on #{message_id}", "   ".join(f"[{b.text}]" for b in buttons))

    def answer(self, callback_id: str, text: str) -> None:
        self.answers.append({"id": callback_id, "text": text})
        self._say("TELEGRAM popup", text)


def format_followup_card(msg: Message, lead: Lead, total_steps: int, previous: Message | None) -> str:
    """A short card: you have already seen the signature and the evidence on email 1, so only what is NEW is shown."""
    from zypher_agent.drafting.email import greeting_for

    ev = msg.evidence
    violations = ev["guardrails"]["violations"]
    status = ("⚠️ FAILED CHECKS (Approve is disabled): " + "; ".join(f"{v['rule']}" for v in violations)
              if violations else "✅ Passed all checks")
    when = f", sent {previous.sent_at:%a %d %b}" if previous is not None and previous.sent_at else ""
    head = (f"🔁 Follow-up {msg.step - 1} · email {msg.step} of {total_steps} · {lead.company_name} ({lead.domain})\n"
            f"To: {lead.contact_name or 'there'} <{lead.email}>"
            + (f"\nEmail {msg.step - 1} was: \"{previous.subject}\"{when}" if previous is not None else ""))
    body = ev["draft"]["body"].strip()
    mail = f"{FOLLOWUP_BANNER}\nSubject: {msg.subject} (sent in the same thread)\n\n{greeting_for(lead.contact_name)}\n\n{body}"
    cited = {c["evidence_id"] for c in ev["draft"].get("claims", [])}
    notes = [f"{h['id']}: “{h['quote'][:110]}”" for h in ev["hypotheses"] if h["id"] in cited]
    text = "\n\n".join([head, status, mail] + (["📝 Based on: " + " | ".join(notes)] if notes else []))
    return text[:CARD_LIMIT]


def format_card(msg: Message, lead: Lead, total_steps: int = 4, previous: Message | None = None) -> str:
    if msg.step > 1:
        return format_followup_card(msg, lead, total_steps, previous)
    ev = msg.evidence
    violations = ev["guardrails"]["violations"]
    status = (
        "⚠️ FAILED CHECKS (Approve is disabled):\n" + "\n".join(f"- {v['rule']}: {v['detail']}" for v in violations)
        if violations else "✅ Passed all checks"
    )
    head = f"📧 Draft v{msg.version} · {lead.company_name} ({lead.domain})\nTo: {lead.contact_name or 'there'} <{lead.email}>"
    if lead.phone:  # for a follow-up call or WhatsApp by a human
        head += f"\nPhone: {lead.phone}"
    # The card has two zones. Only the first is sent to the lead; the second is the reviewer's notes.
    mail = f"{EMAIL_BANNER}\nSubject: {msg.subject}\n\n{msg.body}\n{END_BANNER}"

    def evidence(quotes: bool) -> str:
        lines = []
        for h in ev["hypotheses"]:
            line = f"{h['id']} ({h['confidence']}): {h['hypothesis']}"
            lines.append(line + (f"\n   “{h['quote']}” — {h['source_url']}" if quotes else ""))
        fit = [f"🏢 Why they fit ({q['kind'].replace('_', ' ')}): “{q['quote'][:160]}”" for q in ev.get("qualification", [])[:2]]
        return f"{NOTES_BANNER}\n" + ("\n".join(fit) + "\n\n" if fit else "") + "Evidence:\n" + "\n".join(lines)

    for with_quotes in (True, False):
        text = "\n\n".join([head, status, mail, evidence(with_quotes)])
        if len(text) <= CARD_LIMIT:
            return text
    return text[:CARD_LIMIT]


class ApprovalService:
    def __init__(
        self, repo: Repository, llm: LLM, icp: Icp, footer: Footer, notifier: Notifier,
        allowed_user_ids: list[int], chat_id: int | None = None, max_open_cards: int = 10,
    ) -> None:
        if not allowed_user_ids:  # an empty allowlist must mean "nobody", never "everybody"
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS is empty: refusing to start with no authorized user")
        self.repo, self.llm, self.icp, self.footer, self.notifier = repo, llm, icp, footer, notifier
        self.allowed = set(allowed_user_ids)
        # Cards go to a shared group if configured (partners), else the first allowed user's private chat.
        # Either way, only allowlisted user ids can press buttons or give feedback.
        self.chat_id = chat_id if chat_id is not None else allowed_user_ids[0]
        self.max_open_cards = max_open_cards
        # Says when an approved email will go out. The CLI sets it from the send worker; None = generic wording.
        self.send_explainer: Callable[[Message, Lead], str] | None = None
        # Returns a reason when sending to this lead is currently held (test mode), so we never take an approval
        # that cannot lead to a send. None = fine. Set by the CLI from the send worker.
        self.approval_gate: Callable[[Lead], str | None] | None = None
        self.sending_status: Callable[[], str] | None = None  # set by the CLI: today's count and cap for /status

    # ---------- outgoing ----------

    def deliver(self, message_id: str) -> None:
        msg = self.repo.get_message(message_id)
        lead = self.repo.get_lead(msg.contact_id)
        self._send_card(msg, lead)
        self.repo.set_lead_status(lead.contact_id, "awaiting_approval")

    @staticmethod
    def _card_buttons(msg: Message) -> list[Button]:
        passed = not msg.evidence["guardrails"]["violations"]
        key = f"{msg.id}|{msg.version}"
        buttons = [Button(text="✅ Approve", data=f"approve|{key}")] if passed else []
        return buttons + [Button(text="💡 Suggest changes", data=f"suggest|{key}"), Button(text="❌ Reject", data=f"reject|{key}")]

    def _send_card(self, msg: Message, lead: Lead) -> None:
        buttons = self._card_buttons(msg)
        sent = [m for m in self.repo.sent_messages(lead.contact_id) if m.step < msg.step]
        card = format_card(msg, lead, 1 + len(self.icp.sequence), sent[-1] if sent else None)
        tg_id = self.notifier.send(self.chat_id, card, buttons)
        self.repo.update_message(
            msg.id, status="awaiting_approval", telegram_chat_id=self.chat_id, telegram_message_id=tg_id
        )

    def _when(self, msg: Message, lead: Lead) -> str:
        """One sentence on what happens next to an approved email."""
        if self.send_explainer is None:
            return "It goes out when the sender is running and the sending rules allow."
        try:
            return self.send_explainer(self.repo.get_message(msg.id), lead)
        except Exception as e:  # noqa: BLE001 - the explanation is a courtesy; approval already happened
            self.repo.log_event("explain_failed", contact_id=lead.contact_id, payload={"error": f"{type(e).__name__}"})
            return "It goes out when the sender is running and the sending rules allow."

    # ---------- the queue behind the cards ----------

    OPEN = ("awaiting_approval", "needs_changes")
    _release_lock = threading.Lock()  # the bot and the worker may release cards at the same moment: never twice

    def open_cards(self) -> int:
        return len(self.repo.messages_with_status(list(self.OPEN)))

    def queued(self) -> list[tuple[Message, Lead]]:
        """Drafts written but not shown yet, most urgent first: follow-ups (people who already know us) before
        new first emails, oldest first within each group."""
        out = []
        for m in self.repo.messages_with_status(["draft"]):
            if m.direction != "outbound":
                continue
            lead = self.repo.get_lead(m.contact_id)
            if lead.status == "drafted":
                out.append((m, lead))
        return sorted(out, key=lambda t: (-t[0].step,))

    def release_queue(self) -> int:
        """Show queued drafts as cards until `max_open_cards` are waiting for you. Returns how many were shown.

        Called after every run, every time you clear a card, and by the worker, so the next card appears as soon
        as there is room. Nobody who has opted out since drafting is ever shown."""
        with self._release_lock:
            return self._release()

    def _release(self) -> int:
        shown = 0
        for msg, lead in self.queued():
            if self.repo.is_suppressed(lead.email, lead.domain):
                self.repo.update_message(msg.id, status="rejected", last_error="opted out before review")
                self.repo.set_lead_status(lead.contact_id, "unsubscribed")
                self.repo.log_event("skipped_suppressed", contact_id=lead.contact_id, message_id=msg.id, actor="system")
                continue
            if self.open_cards() >= self.max_open_cards:
                break
            self.deliver(msg.id)
            shown += 1
        return shown

    def queue_summary(self) -> str:
        queued = self.queued()
        follow = sum(m.step > 1 for m, _ in queued)
        return (f"📋 Waiting for your review: {self.open_cards()} of {self.max_open_cards} slots\n"
                f"Queued behind them: {len(queued)} ({follow} follow-ups, {len(queued) - follow} new first emails)\n"
                f"A new card appears as soon as you approve, reject or change one.")

    # ---------- incoming ----------

    def handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self._callback(update["callback_query"])
            elif update.get("message", {}).get("text"):
                self._text(update["message"])
        except Exception as e:  # one bad update must not kill the bot
            self.repo.log_event("approval_error", payload={"error": f"{type(e).__name__}: {e}"})
            try:
                self.notifier.send(self.chat_id, "⚠️ Something went wrong handling that. It has been logged.")
            except Exception:
                pass

    def _authorized(self, user_id: int | None, what: str) -> bool:
        if user_id in self.allowed:
            return True
        self.repo.log_event("unauthorized", actor=f"telegram:{user_id}", payload={"attempt": what})
        return False

    def _callback(self, cq: dict) -> None:
        if not self._authorized(cq.get("from", {}).get("id"), "callback"):
            return
        cb_id, user = cq["id"], cq["from"]["id"]
        chat_id, tg_msg_id = cq["message"]["chat"]["id"], cq["message"]["message_id"]
        parts = str(cq.get("data", "")).split("|")
        try:
            action, message_id, version = parts[0], str(uuid.UUID(parts[1])), int(parts[2])
            if action not in ("approve", "confirm", "cancel", "suggest", "reject") or len(parts) != 3:
                raise ValueError
            msg = self.repo.get_message(message_id)
        except (ValueError, IndexError, KeyError):
            self.notifier.answer(cb_id, "I don't recognise that draft.")
            return

        if msg.version != version:
            self.notifier.answer(cb_id, f"That card is out of date (current is v{msg.version}).")
            return
        actor = f"human:{user}"
        lead = self.repo.get_lead(msg.contact_id)

        if action == "cancel":
            if msg.status == "awaiting_approval":
                self.notifier.set_buttons(chat_id, tg_msg_id, self._card_buttons(msg))
            self.notifier.answer(cb_id, "Cancelled. Nothing was approved.")
            return

        if action in ("approve", "confirm"):
            if msg.status != "awaiting_approval":
                self.notifier.answer(cb_id, f"Already {msg.status}.")
                return
            check = self._recheck(msg)
            if not check.passed:
                self.notifier.answer(cb_id, "Blocked: " + "; ".join(v.rule for v in check.violations))
                return
            if self.repo.is_suppressed(lead.email, lead.domain):  # they may have opted out since drafting
                self.notifier.answer(cb_id, "Blocked: this contact is on the do-not-contact list.")
                return
            held = self.approval_gate(lead) if self.approval_gate is not None else None
            if held:  # explain now, instead of letting the owner approve something that can never be sent
                self.notifier.answer(cb_id, held[:195])
                return
            if action == "approve":  # sending cannot be undone, so ask once more, naming exactly who gets it
                key = f"{msg.id}|{msg.version}"
                address = (lead.email or "")[:38]
                self.notifier.set_buttons(chat_id, tg_msg_id, [
                    Button(text=f"✅ Yes, send to {address}", data=f"confirm|{key}"),
                    Button(text="↩ Cancel", data=f"cancel|{key}"),
                ])
                self.notifier.answer(cb_id, f"Send to {lead.email} ({lead.company_name})? Tap Yes to confirm.")
                return
            self.repo.update_message(msg.id, status="approved")
            self.repo.set_lead_status(lead.contact_id, "approved")
            self.repo.log_event("approved", contact_id=lead.contact_id, message_id=msg.id, actor=actor, payload={"version": msg.version})
            when = self._when(msg, lead)
            self.notifier.edit(
                chat_id, tg_msg_id,
                f"✅ APPROVED v{msg.version} for {lead.email}\n{when}\n\nSubject: {msg.subject}\n\n{msg.body}",
            )
            self.notifier.answer(cb_id, "✅ Approved")

        elif action == "reject":
            if msg.status not in ("awaiting_approval", "needs_changes"):
                self.notifier.answer(cb_id, f"Already {msg.status}.")
                return
            # Reject means "this DRAFT is not good", not "never contact this company": the lead goes back to
            # be redrafted on the next run. To drop a company for good, use /suppress with its domain.
            self.repo.update_message(msg.id, status="rejected")
            # A rejected FIRST email is redrafted. A rejected follow-up returns the lead to its sequence, so the
            # scheduler writes a fresh follow-up: it must never restart at step 1 for someone already emailed.
            self.repo.set_lead_status(lead.contact_id, "researched" if msg.step == 1 else "in_sequence")
            self.repo.log_event("rejected", contact_id=lead.contact_id, message_id=msg.id, actor=actor, payload={"version": msg.version})
            self.notifier.edit(
                chat_id, tg_msg_id,
                f"❌ REJECTED v{msg.version}. A new draft will be written on the next run.\n"
                f"To never contact them: /suppress {lead.domain}\n\nSubject: {msg.subject}",
            )
            self.notifier.answer(cb_id, "❌ Rejected: will redraft next run")

        else:  # suggest
            if msg.status != "awaiting_approval":
                self.notifier.answer(cb_id, f"Can't change a draft that is {msg.status}.")
                return
            self.repo.update_message(msg.id, status="needs_changes", telegram_chat_id=chat_id, telegram_message_id=tg_msg_id)
            self.repo.log_event("changes_requested", contact_id=lead.contact_id, message_id=msg.id, actor=actor)
            self.notifier.send(chat_id, "✏️ Type what to change. Examples: make it shorter · warmer tone · lead with the hiring signal")
            self.notifier.answer(cb_id, "Okay, tell me what to change.")

    def _text(self, m: dict) -> None:
        if not self._authorized(m.get("from", {}).get("id"), "message"):
            return
        chat_id, user, text = m["chat"]["id"], m["from"]["id"], m["text"].strip()
        command = text.split(" ")[0].split("@")[0]  # "/suppress@BotName x" is how groups send it
        if command == "/suppress":
            self._suppress(chat_id, user, text.split(maxsplit=1)[1] if " " in text else "")
            return
        if command == "/stats":
            from zypher_agent.sender.stats import build_report, format_report

            self.notifier.send(chat_id, "📊 " + format_report(build_report(self.repo)))
            return
        if command == "/queue":
            self.notifier.send(chat_id, self.queue_summary())
            return
        if command in ("/pause", "/resume", "/status"):
            self._control(command, chat_id, user)
            return
        msg = None if text.startswith("/") else self.repo.message_needing_changes(chat_id)
        if msg is None:
            self.notifier.send(chat_id, HELP)
            return

        lead = self.repo.get_lead(msg.contact_id)
        report = self.repo.latest_research(lead.company_id)
        self.notifier.send(chat_id, "✏️ Redrafting…")
        try:
            result = generate(
                lead, report, self.icp, self.llm, self.footer, previous=EmailDraft(**msg.evidence["draft"]),
                feedback=text, step=msg.step, sent=self._history(msg),
            )
        except LLMError as e:
            self.repo.log_event("redraft_failed", contact_id=lead.contact_id, message_id=msg.id, actor=f"human:{user}", payload={"error": str(e)})
            self.notifier.send(chat_id, f"Redraft failed ({e}). Send your feedback again.")
            return

        new = self.repo.update_message(
            msg.id, subject=result.draft.subject, body=result.text, version=msg.version + 1, feedback=text,
            evidence=evidence_for(result, report), status="awaiting_approval",
        )
        self.repo.log_event(
            "redrafted", contact_id=lead.contact_id, message_id=msg.id, actor=f"human:{user}",
            payload={"from_version": msg.version, "feedback": text, "old_subject": msg.subject, "old_body": msg.body},
        )
        if msg.telegram_message_id:
            self.notifier.edit(chat_id, msg.telegram_message_id, f"✏️ Superseded by v{new.version}. Feedback: {text[:300]}")
        self._send_card(new, lead)

    def refresh_pending(self) -> int:
        """Redraft every draft still waiting on a human, with the current prompt and rules, and replace its card.

        Used after the prompt or guardrails improve, so stale drafts never get approved. Each draft gets
        the full check again; the old card is marked superseded, and its buttons stop working because
        they carry the old version number. Returns how many drafts were refreshed.
        """
        count = 0
        for msg in self.repo.messages_with_status(["awaiting_approval", "needs_changes"]):
            lead = self.repo.get_lead(msg.contact_id)
            if self.repo.is_suppressed(lead.email, lead.domain):
                self.repo.update_message(msg.id, status="rejected")
                self.repo.log_event("skipped_suppressed", contact_id=lead.contact_id, message_id=msg.id, actor="system")
                continue
            report = self.repo.latest_research(lead.company_id)
            if report is None:
                continue
            try:
                result = generate(lead, report, self.icp, self.llm, self.footer, step=msg.step, sent=self._history(msg))
            except LLMError as e:
                self.repo.log_event("refresh_failed", contact_id=lead.contact_id, message_id=msg.id, payload={"error": str(e)})
                continue
            new = self.repo.update_message(
                msg.id, subject=result.draft.subject, body=result.text, version=msg.version + 1,
                feedback="auto-refresh: prompt and rules updated", evidence=evidence_for(result, report),
                status="awaiting_approval",
            )
            self.repo.log_event(
                "refreshed", contact_id=lead.contact_id, message_id=msg.id, actor="system",
                payload={"from_version": msg.version, "old_subject": msg.subject, "old_body": msg.body},
            )
            if msg.telegram_message_id:
                try:
                    self.notifier.edit(
                        msg.telegram_chat_id or self.chat_id, msg.telegram_message_id,
                        f"♻️ Superseded by v{new.version} (improved wording). This card is out of date.",
                    )
                except Exception as e:  # an old card we can no longer edit must not stop the refresh
                    self.repo.log_event("refresh_edit_failed", message_id=msg.id, payload={"error": str(e)})
            self._send_card(new, lead)
            count += 1
        return count

    def _history(self, msg: Message) -> list[tuple[int, str, str]]:
        """Earlier emails of this sequence, so a redrafted follow-up still adds something new."""
        return [(m.step, m.subject, m.body) for m in self.repo.sent_messages(msg.contact_id)]

    def alert(self, text: str) -> None:
        """Tell the owner something that needs attention (bounce, failed send, paused sending)."""
        self.notifier.send(self.chat_id, "🔔 " + text)

    def _control(self, command: str, chat_id: int, user: int) -> None:
        """Pause, resume or inspect sending. Only a human can resume after a stop-loss pause."""
        if command == "/pause":
            self.repo.set_sending_paused(True, f"paused by human:{user}")
            self.repo.log_event("sending_paused", actor=f"human:{user}", payload={"reason": "manual"})
            self.notifier.send(chat_id, "⏸ Sending is paused. Nothing will leave until you send /resume.")
        elif command == "/resume":
            self.repo.set_sending_paused(False)
            self.repo.log_event("sending_resumed", actor=f"human:{user}")
            self.notifier.send(chat_id, "▶️ Sending resumed. The worker picks up approved emails on its next pass.")
        else:
            paused, reason = self.repo.sending_state()
            line = f"⏸ PAUSED: {reason}" if paused else "▶️ Sending is on."
            waiting = len(self.repo.messages_with_status(["awaiting_approval", "needs_changes"]))
            ready = len(self.repo.messages_with_status(["approved"]))
            sent = len(self.repo.messages_with_status(["sent"]))
            extra = f"\n{self.sending_status()}" if self.sending_status else ""
            self.notifier.send(chat_id, f"{line}\nWaiting for your approval: {waiting}\nApproved, waiting to send: {ready}\nSent so far: {sent}{extra}")

    def _suppress(self, chat_id: int, user: int, target: str) -> None:
        """Opt-outs are honoured immediately: research, drafting, approval (and later sending) all check this list."""
        target = target.strip()
        email = clean_email(target) if "@" in target else None
        domain = None if email else normalize_domain(target)
        if not email and not domain:
            self.notifier.send(chat_id, "Usage: /suppress name@company.com  or  /suppress company.com")
            return
        self.repo.add_suppression("manual", email=email, domain=domain)
        self.repo.log_event("suppressed", actor=f"human:{user}", payload={"email": email, "domain": domain})
        self.notifier.send(chat_id, f"🚫 {email or domain} is on the do-not-contact list. They will never be emailed.")

    def _recheck(self, msg: Message):
        ev = msg.evidence
        return check_for_step(
            EmailDraft(**ev["draft"]), [Hypothesis(**h) for h in ev["hypotheses"]], self.icp, self.footer, msg.step
        )

"""Approve asks 'send to whom?' first. Only the second tap approves, and the card then says when it goes out."""
import random
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from test_approval import FOOTER, GOOD, ICP, OWNER, STRANGER, approve, popup, tap, world
from test_guardrails import GOOD_BODY
from test_queue import queued_world

from zypher_agent.approval import ApprovalService
from zypher_agent.config import Settings
from zypher_agent.db import InMemoryRepo
from zypher_agent.llm import MockLLM
from zypher_agent.models import CompanyIn, ContactIn, NewMessage
from zypher_agent.sender.transport import FakeTransport
from zypher_agent.sender.worker import SendWorker

DUBAI = ZoneInfo("Asia/Dubai")
TUE_10AM = datetime(2026, 10, 20, 10, 0, tzinfo=DUBAI).astimezone(UTC)


def buttons(notifier):
    return [b.text for b in notifier.sent[-1]["buttons"]]


def test_the_first_tap_only_asks_and_approves_nothing():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "approve")
    assert repo.get_message(msg.id).status == "awaiting_approval" and not any(e["type"] == "approved" for e in repo.events)
    assert buttons(notifier) == ["✅ Yes, send to ann@acme.com", "↩ Cancel"]
    assert "ann@acme.com" in popup(notifier) and "Tap Yes" in popup(notifier)
    assert len(notifier.sent) == 1  # the same card is reused: no extra message in the chat


def test_cancel_puts_the_three_original_buttons_back():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "approve")
    tap(service, msg, "cancel")
    assert buttons(notifier) == ["✅ Approve", "💡 Suggest changes", "❌ Reject"]
    assert repo.get_message(msg.id).status == "awaiting_approval" and "Nothing was approved" in popup(notifier)


def test_the_second_tap_approves_and_the_card_says_what_happens_next():
    repo, notifier, service, msg, _ = world()
    service.send_explainer = lambda m, lead: "It goes out today at 10:40 their time (test)."
    approve(service, msg)
    assert repo.get_message(msg.id).status == "approved"
    assert sum(e["type"] == "approved" for e in repo.events) == 1
    assert "goes out today at 10:40" in notifier.edits[-1]["text"] and "ann@acme.com" in notifier.edits[-1]["text"]


def test_without_a_sender_the_card_still_says_something_honest():
    _, notifier, service, msg, _ = world()
    approve(service, msg)
    assert "when the sender is running" in notifier.edits[-1]["text"]


def test_a_broken_explainer_never_blocks_the_approval():
    repo, notifier, service, msg, _ = world()
    service.send_explainer = lambda m, lead: 1 / 0
    approve(service, msg)
    assert repo.get_message(msg.id).status == "approved" and any(e["type"] == "explain_failed" for e in repo.events)


def test_confirm_checks_everything_again_at_the_moment_of_the_second_tap():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "approve")
    repo.add_suppression("manual", email="ann@acme.com")  # they opt out between the two taps
    tap(service, msg, "confirm")
    assert repo.get_message(msg.id).status == "awaiting_approval" and "do-not-contact" in popup(notifier)


def test_a_stale_or_stranger_confirm_does_nothing():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "approve")
    tap(service, msg, "confirm", version=msg.version + 1)
    assert "out of date" in popup(notifier)
    tap(service, msg, "confirm", user=STRANGER)
    assert repo.get_message(msg.id).status == "awaiting_approval"
    assert any(e["type"] == "unauthorized" for e in repo.events)


def test_confirm_cannot_approve_a_card_that_failed_its_checks():
    bad = GOOD.model_copy(update={"body": GOOD_BODY + " Revolutionary."})
    repo, notifier, service, msg, _ = world(draft=bad)
    tap(service, msg, "confirm")  # a forged tap: the failed card never offered the button
    assert repo.get_message(msg.id).status == "awaiting_approval" and "Blocked" in popup(notifier)


# ---------- the send-time sentence ----------

class W:
    """A worker over approved emails, to ask it when an email will leave."""

    def __init__(self, **settings):
        self.repo = InMemoryRepo()
        base = {"_env_file": None, "sending_enabled": True, "sender_email": FOOTER.sender_email}
        self.worker = SendWorker(self.repo, FakeTransport(), Settings(**{**base, **settings}), FOOTER, ICP, rng=random.Random(1))

    def approved(self, country="UAE", email="o@acme.ae", name="Acme"):
        cid = self.repo.upsert_lead(CompanyIn(name=name, domain=email.split("@")[1], country=country), ContactIn(email=email))
        m = self.repo.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
        self.repo.update_message(m.id, status="approved")
        return self.repo.get_message(m.id), self.repo.get_lead(cid)

    def say(self, now=TUE_10AM, **kw):
        m, lead = self.approved(**kw)
        return self.worker.explain(m, lead, now)


def test_it_works_without_a_given_time_as_the_bot_calls_it():
    w = W()
    m, lead = w.approved()
    text = w.worker.explain(m, lead)  # the bot never passes a time; this used to crash and fall back to vague text
    assert text and "goes out when the sender is running" not in text


def test_inside_the_window_it_says_within_a_minute():
    assert "within about a minute" in W().say()


def test_outside_the_window_it_names_the_day_and_time_in_their_clock():
    saturday = datetime(2026, 10, 24, 10, 0, tzinfo=DUBAI).astimezone(UTC)
    text = W().say(saturday)
    assert "Mon 26 Oct at 09:00 their time" in text and "business hours" in text


def test_when_todays_limit_is_used_up_it_says_so():
    w = W(daily_send_cap=1)
    first, _ = w.approved(email="a@one.ae", name="One")
    w.repo.update_message(first.id, status="sent", sent_at=TUE_10AM - timedelta(minutes=30))
    assert "daily cap" in w.say(email="b@two.ae", name="Two")


def test_approved_emails_ahead_of_it_count_against_todays_limit():
    w = W(daily_send_cap=2)
    w.approved(email="a@one.ae", name="One")
    w.approved(email="b@two.ae", name="Two")
    assert "daily cap" in w.say(email="c@three.ae", name="Three")  # two others are ahead in a limit of 2


def test_it_says_plainly_when_sending_is_off_paused_or_in_test_mode():
    assert "switched OFF" in W(sending_enabled=False).say()
    paused = W()
    paused.repo.set_sending_paused(True, "bounce spike")
    text = paused.say()
    assert "PAUSED" in text and "/resume" in text
    assert "TEST MODE" in W(send_allowlist="me@gmail.com").say()


def test_every_real_send_is_announced_in_telegram():
    from test_send_worker import World

    w = World()
    alerts = []
    w.worker.alert = alerts.append
    w.message(w.lead(name="Acme", email="o@acme.ae"))
    w.run()
    assert alerts and alerts[-1].startswith("✅ Sent to o@acme.ae (Acme)")


# ---------- two programs releasing cards at once ----------

def test_two_threads_releasing_the_queue_never_show_the_same_card_twice():
    repo, notifier, _, _ = queued_world(first_emails=30, cap=50)
    services = [ApprovalService(repo, MockLLM(repo, {}), ICP, FOOTER, notifier, [OWNER], max_open_cards=50) for _ in range(2)]
    threads = [threading.Thread(target=services[i % 2].release_queue) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(notifier.sent) == 30  # 30 queued drafts, 30 cards: none shown twice
    assert len(repo.messages_with_status(["awaiting_approval"])) == 30


# ---------- test mode must never fail silently ----------

def test_approving_an_address_that_test_mode_would_hold_is_refused_with_the_reason():
    repo, notifier, service, msg, _ = world()
    w = W(send_allowlist="me@gmail.com")
    service.approval_gate = lambda lead: w.worker.held_reason(lead.email)
    tap(service, msg, "approve")
    assert repo.get_message(msg.id).status == "awaiting_approval"           # nothing approved
    assert "TEST MODE" in popup(notifier) and "me@gmail.com" in popup(notifier) and "SEND_ALLOWLIST" in popup(notifier)
    assert [b.text for b in notifier.sent[-1]["buttons"]][0] == "✅ Approve"  # the card is unchanged
    tap(service, msg, "confirm")  # a forged second tap is refused too
    assert repo.get_message(msg.id).status == "awaiting_approval"


def test_the_gate_lets_allowed_addresses_through():
    repo, notifier, service, msg, _ = world()
    w = W(send_allowlist="ann@acme.com")
    service.approval_gate = lambda lead: w.worker.held_reason(lead.email)
    approve(service, msg)
    assert repo.get_message(msg.id).status == "approved"


def test_an_already_approved_email_held_by_test_mode_is_reported_once_not_silently():
    from test_send_worker import World
    w = World(send_allowlist="someone@else.com")
    alerts = []
    w.worker.alert = alerts.append
    w.message(w.lead(name="Acme", email="o@acme.ae"))
    first = w.run()
    again = w.run(TUE_10AM + timedelta(minutes=5))
    assert first.sent == 0 and first.blocked and "TEST MODE" in first.blocked[0]       # printed in the terminal
    assert len(alerts) == 1 and alerts[0].startswith("⏸ Approved but NOT sent: o@acme.ae")  # told once in Telegram
    assert again.blocked and len(alerts) == 1                                           # still visible, not repeated

"""The review queue: you never see more than `max_open_cards`, follow-ups go first, and the next card appears
the moment you clear one."""
from datetime import UTC, datetime

from test_approval import FOOTER, GOOD, ICP, OWNER, REPORT, popup, say, tap

from zypher_agent.approval import ApprovalService, ConsoleNotifier, format_card
from zypher_agent.db import InMemoryRepo
from zypher_agent.drafting.email import DraftResult, evidence_for, render_email
from zypher_agent.guardrails.checks import check_draft
from zypher_agent.integrations.telegram import poll_once
from zypher_agent.llm import MockLLM
from zypher_agent.models import CompanyIn, ContactIn, NewMessage


def queued_world(first_emails=12, follow_ups=0, cap=10):
    repo, notifier = InMemoryRepo(), ConsoleNotifier(echo=False)
    service = ApprovalService(repo, MockLLM(repo, {}), ICP, FOOTER, notifier, [OWNER], max_open_cards=cap)
    ids = []

    def add(i, step):
        cid = repo.upsert_lead(CompanyIn(name=f"Co{i}", domain=f"co{i}.ae", country="UAE"), ContactIn(full_name="Ann Lee", email=f"o@co{i}.ae"))
        lead = repo.get_lead(cid)
        repo.save_research(lead.company_id, REPORT)
        check = check_draft(GOOD, REPORT.hypotheses, ICP, FOOTER)
        res = DraftResult(GOOD, check, 1, render_email(GOOD, "Ann Lee", FOOTER))
        if step > 1:  # an email 1 that already went out
            first = repo.save_message(NewMessage(contact_id=cid, subject="quote requests", body="b"))
            repo.update_message(first.id, status="sent", sent_at=datetime(2026, 10, 20, tzinfo=UTC), message_id_header=f"<f{i}@x>")
        m = repo.save_message(NewMessage(contact_id=cid, subject=GOOD.subject, body=res.text, evidence=evidence_for(res, REPORT), step=step))
        repo.set_lead_status(cid, "drafted")
        ids.append(m.id)
        return m

    for i in range(follow_ups):
        add(f"f{i}", 2)
    for i in range(first_emails):
        add(i, 1)
    return repo, notifier, service, ids


def test_no_more_than_the_limit_of_cards_are_ever_open():
    repo, notifier, service, _ = queued_world(first_emails=15)
    assert service.release_queue() == 10 and service.open_cards() == 10 and len(notifier.sent) == 10
    assert service.release_queue() == 0 and len(notifier.sent) == 10  # running again adds nothing
    assert len(service.queued()) == 5  # five wait quietly, none are lost


def test_follow_ups_are_shown_before_new_first_emails():
    repo, notifier, service, _ = queued_world(first_emails=10, follow_ups=3, cap=5)
    service.release_queue()
    shown = [repo.get_message(m).step for m in [x.id for x in repo.messages_with_status(["awaiting_approval"])]]
    assert sorted(shown, reverse=True)[:3] == [2, 2, 2] and len(shown) == 5


def test_clearing_a_card_brings_the_next_one_without_running_anything():
    repo, notifier, service, ids = queued_world(first_emails=12, cap=10)
    service.release_queue()
    waiting = repo.messages_with_status(["awaiting_approval"])
    tap(service, waiting[0], "reject")
    assert service.release_queue() == 1 and service.open_cards() == 10  # room for exactly one more


def test_the_bot_shows_the_next_card_as_soon_as_you_tap():
    repo, notifier, service, _ = queued_world(first_emails=12, cap=10)
    service.release_queue()
    msg = repo.messages_with_status(["awaiting_approval"])[0]

    class Tg:
        def get_updates(self, offset, timeout):
            return [{"update_id": 7, "callback_query": {
                "id": "cb", "from": {"id": OWNER}, "message": {"message_id": 1, "chat": {"id": OWNER}},
                "data": f"reject|{msg.id}|{msg.version}"}}]

    assert poll_once(Tg(), service, None) == 8
    assert service.open_cards() == 10 and len(notifier.sent) == 11  # one cleared, one released


def test_a_card_waiting_for_your_typed_feedback_still_counts_as_open():
    repo, notifier, service, _ = queued_world(first_emails=12, cap=10)
    service.release_queue()
    tap(service, repo.messages_with_status(["awaiting_approval"])[0], "suggest")
    assert service.open_cards() == 10 and service.release_queue() == 0


def test_someone_who_opted_out_while_queued_is_never_shown():
    repo, notifier, service, ids = queued_world(first_emails=3, cap=10)
    repo.add_suppression("manual", email="o@co1.ae")
    assert service.release_queue() == 2
    gone = next(m for m in repo.messages.values() if repo.get_lead(m.contact_id).email == "o@co1.ae")
    assert gone.status == "rejected"


def test_the_queue_command_says_what_is_waiting_and_what_is_behind_it():
    repo, notifier, service, _ = queued_world(first_emails=9, follow_ups=3, cap=10)
    service.release_queue()
    say(service, "/queue")
    text = notifier.sent[-1]["text"]
    assert "10 of 10" in text and "Queued behind them: 2 (0 follow-ups, 2 new first emails)" in text


def test_a_limit_of_zero_shows_nothing_and_loses_nothing():
    repo, notifier, service, _ = queued_world(first_emails=4, cap=0)
    assert service.release_queue() == 0 and notifier.sent == [] and len(service.queued()) == 4


# ---------- the short follow-up card ----------

def test_a_follow_up_card_is_short_and_says_where_it_is_in_the_sequence():
    repo, notifier, service, _ = queued_world(first_emails=0, follow_ups=1)
    service.release_queue()
    card = notifier.sent[-1]["text"]
    assert "Follow-up 1" in card and "email 2 of 4" in card and 'Email 1 was: "quote requests"' in card
    assert "Mon 20 Oct" in card or "Tue 20 Oct" in card  # when the previous email left
    assert "Hi Ann," in card and GOOD.body.split(".")[0] in card
    assert FOOTER.postal_address not in card and "Evidence:" not in card and "ONLY FOR YOU" not in card
    first_email_card = format_card(repo.messages_with_status(["awaiting_approval"])[0].model_copy(update={"step": 1}), repo.get_lead(repo.messages_with_status(["awaiting_approval"])[0].contact_id))
    assert len(card) < len(first_email_card) * 0.7  # clearly shorter than a full card


def test_a_follow_up_card_still_has_all_three_buttons_and_dead_buttons_are_refused():
    repo, notifier, service, _ = queued_world(first_emails=0, follow_ups=1)
    service.release_queue()
    assert [b.text for b in notifier.sent[-1]["buttons"]] == ["✅ Approve", "💡 Suggest changes", "❌ Reject"]
    msg = repo.messages_with_status(["awaiting_approval"])[0]
    tap(service, msg, "approve", version=msg.version + 1)
    assert "out of date" in popup(notifier)

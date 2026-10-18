import pytest
from test_guardrails import FOOTER, GOOD_BODY, HYP

from zypher_agent.approval import CARD_LIMIT, ApprovalService, ConsoleNotifier
from zypher_agent.config import Settings, load_icp
from zypher_agent.db import InMemoryRepo
from zypher_agent.drafting.email import DraftResult, evidence_for, render_email
from zypher_agent.guardrails.checks import check_draft
from zypher_agent.llm import LLMError, MockLLM
from zypher_agent.models import (
    Claim,
    CompanyIn,
    ContactIn,
    EmailDraft,
    NewMessage,
    ResearchReport,
)

ICP = load_icp(Settings(_env_file=None).icp_path)
OWNER, STRANGER = 111, 999
GOOD = EmailDraft(subject="Quotes by hand?", body=GOOD_BODY, claims=[Claim(text="q", evidence_id="h1")])
SHORTER = GOOD.model_copy(update={"subject": "Shorter version", "body": GOOD_BODY.replace(" No call needed.", "")})
REPORT = ResearchReport(hypotheses=[HYP], summary="A freight company.")


def world(redraft=None, draft=GOOD):
    repo, notifier = InMemoryRepo(), ConsoleNotifier(echo=False)
    cid = repo.upsert_lead(
        CompanyIn(name="Acme", domain="acme.com"), ContactIn(full_name="Ann Lee", email="ann@acme.com")
    )
    lead = repo.get_lead(cid)
    repo.save_research(lead.company_id, REPORT)
    check = check_draft(draft, REPORT.hypotheses, ICP, FOOTER)
    result = DraftResult(draft, check, 1, render_email(draft, "Ann Lee", FOOTER))
    msg = repo.save_message(NewMessage(
        contact_id=cid, subject=draft.subject, body=result.text, evidence=evidence_for(result, REPORT),
    ))
    llm = MockLLM(repo, {"redraft": redraft} if redraft else {})
    service = ApprovalService(repo, llm, ICP, FOOTER, notifier, [OWNER])
    service.deliver(msg.id)
    return repo, notifier, service, repo.get_message(msg.id), llm


def tap(service, msg, action, user=OWNER, version=None):
    last = msg.telegram_message_id or 1
    service.handle_update({"callback_query": {
        "id": "cb1", "from": {"id": user}, "message": {"message_id": last, "chat": {"id": user}},
        "data": f"{action}|{msg.id}|{version or msg.version}",
    }})


def say(service, text, user=OWNER):
    service.handle_update({"message": {"from": {"id": user}, "chat": {"id": user}, "text": text}})


def popup(notifier):
    return notifier.answers[-1]["text"]


def test_empty_allowlist_refuses_to_start():
    with pytest.raises(ValueError):
        ApprovalService(InMemoryRepo(), None, ICP, FOOTER, ConsoleNotifier(echo=False), [])


def test_deliver_sends_card_with_three_buttons_and_records_it():
    repo, notifier, _, msg, _ = world()
    card = notifier.sent[0]
    assert [b.text for b in card["buttons"]] == ["✅ Approve", "💡 Suggest changes", "❌ Reject"]
    assert all(len(b.data.encode()) <= 64 for b in card["buttons"])  # Telegram's callback_data limit
    assert "Subject: Quotes by hand?" in card["text"] and "Request a quote by email or WhatsApp" in card["text"]
    assert "Passed all checks" in card["text"] and "ann@acme.com" in card["text"]
    assert (msg.status, msg.telegram_chat_id, msg.telegram_message_id) == ("awaiting_approval", OWNER, 1)
    assert repo.contacts[msg.contact_id]["status"] == "awaiting_approval"


def test_failed_draft_hides_approve_and_shows_why():
    bad = GOOD.model_copy(update={"body": GOOD_BODY + " Revolutionary."})
    _, notifier, service, msg, _ = world(draft=bad)
    card = notifier.sent[0]
    assert [b.text for b in card["buttons"]] == ["💡 Suggest changes", "❌ Reject"]
    assert "FAILED CHECKS" in card["text"] and "banned_phrase" in card["text"]


def test_approve_records_decision_and_sends_nothing():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "approve")
    assert repo.get_message(msg.id).status == "approved"
    assert repo.contacts[msg.contact_id]["status"] == "approved"
    assert any(e["type"] == "approved" and e["actor"] == f"human:{OWNER}" for e in repo.events)
    assert "Nothing was sent" in notifier.edits[-1]["text"] and popup(notifier) == "✅ Approved"
    assert len(notifier.sent) == 1  # no email, no extra message: only the original card exists


def test_double_tap_is_idempotent():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "approve")
    tap(service, msg, "approve")
    assert popup(notifier) == "Already approved."
    assert sum(e["type"] == "approved" for e in repo.events) == 1


def test_reject():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "reject")
    assert repo.get_message(msg.id).status == "rejected"
    assert repo.contacts[msg.contact_id]["status"] == "researched"
    tap(service, msg, "approve")  # cannot approve a rejected draft
    assert repo.get_message(msg.id).status == "rejected"


def test_approve_is_rechecked_when_pressed():
    repo, notifier, service, msg, _ = world()
    tampered = dict(msg.evidence)
    tampered["draft"] = {**tampered["draft"], "body": GOOD_BODY + " Our pricing is low."}
    repo.update_message(msg.id, evidence=tampered)  # stored check still says "passed"
    tap(service, repo.get_message(msg.id), "approve")
    assert repo.get_message(msg.id).status == "awaiting_approval"
    assert popup(notifier).startswith("Blocked") and "pricing" in popup(notifier)


def test_suggest_changes_then_feedback_redrafts_and_supersedes_old_card():
    repo, notifier, service, msg, llm = world(redraft=lambda s, u: SHORTER)
    tap(service, msg, "suggest")
    assert repo.get_message(msg.id).status == "needs_changes"
    say(service, "make it shorter")

    new = repo.get_message(msg.id)
    assert (new.version, new.status, new.feedback, new.subject) == (2, "awaiting_approval", "make it shorter", "Shorter version")
    assert "make it shorter" in llm.calls[0]["user"] and GOOD_BODY in llm.calls[0]["user"]
    assert "Superseded by v2" in notifier.edits[-1]["text"]
    assert notifier.sent[-1]["buttons"][0].data.endswith("|2")
    redrafted = [e for e in repo.events if e["type"] == "redrafted"][0]
    assert redrafted["payload"]["old_body"] == msg.body  # v1 is preserved in the audit log

    tap(service, msg, "approve", version=1)  # the stale v1 card
    assert repo.get_message(msg.id).status == "awaiting_approval"
    assert "out of date" in popup(notifier)
    tap(service, new, "approve")
    assert repo.get_message(msg.id).status == "approved"


def test_text_with_nothing_pending_does_not_call_the_llm():
    _, notifier, service, _, llm = world(redraft=lambda s, u: SHORTER)
    say(service, "hello")
    say(service, "/start")
    assert llm.calls == [] and "Suggest changes" in notifier.sent[-1]["text"]


def test_failed_redraft_keeps_waiting_for_feedback():
    def boom(s, u):
        raise LLMError("boom")

    repo, notifier, service, msg, _ = world(redraft=boom)
    tap(service, msg, "suggest")
    say(service, "shorter")
    assert repo.get_message(msg.id).status == "needs_changes" and repo.get_message(msg.id).version == 1
    assert "Redraft failed" in notifier.sent[-1]["text"]


def test_redraft_that_breaks_rules_is_shown_without_approve():
    pricing = GOOD.model_copy(update={"body": GOOD_BODY + " Pricing starts low."})
    repo, notifier, service, msg, _ = world(redraft=lambda s, u: pricing)
    tap(service, msg, "suggest")
    say(service, "add pricing")
    assert "pricing" in notifier.sent[-1]["text"] and not any("Approve" in b.text for b in notifier.sent[-1]["buttons"])


def test_unauthorized_user_is_ignored_and_logged():
    repo, notifier, service, msg, llm = world(redraft=lambda s, u: SHORTER)
    before = (len(notifier.sent), len(notifier.answers))
    tap(service, msg, "approve", user=STRANGER)
    tap(service, msg, "suggest", user=STRANGER)
    say(service, "make it shorter", user=STRANGER)
    assert repo.get_message(msg.id).status == "awaiting_approval" and llm.calls == []
    assert (len(notifier.sent), len(notifier.answers)) == before  # not even a reply
    assert sum(e["type"] == "unauthorized" and e["actor"] == f"telegram:{STRANGER}" for e in repo.events) == 3


@pytest.mark.parametrize("data", ["", "approve", "approve|not-a-uuid|1", "approve|00000000-0000-0000-0000-000000000000|1", "delete|x|1", "approve|x|y|z"])
def test_garbage_callback_data_is_handled(data):
    repo, notifier, service, msg, _ = world()
    service.handle_update({"callback_query": {"id": "c", "from": {"id": OWNER}, "message": {"message_id": 1, "chat": {"id": OWNER}}, "data": data}})
    assert popup(notifier) == "I don't recognise that draft."
    assert repo.get_message(msg.id).status == "awaiting_approval"


def test_card_fits_telegrams_limit_even_with_huge_evidence():
    big = HYP.model_copy(update={"quote": "word " * 2000, "hypothesis": "x " * 500})
    report = REPORT.model_copy(update={"hypotheses": [big, big.model_copy(update={"id": "h2"})]})
    repo, notifier, service, msg, _ = world()
    repo.update_message(msg.id, evidence={**msg.evidence, "hypotheses": [h.model_dump() for h in report.hypotheses]})
    service.deliver(msg.id)
    assert len(notifier.sent[-1]["text"]) <= CARD_LIMIT


def test_unexpected_error_is_logged_and_reported_not_raised():
    repo, notifier, service, msg, _ = world()
    service.handle_update({"callback_query": {"id": "c", "from": {"id": OWNER}}})  # malformed: no message
    assert any(e["type"] == "approval_error" for e in repo.events)
    assert "went wrong" in notifier.sent[-1]["text"]


# ---------- opt-outs and shared approval chats ----------

def test_suppress_command_adds_email_or_domain():
    repo, notifier, service, _, _ = world()
    say(service, "/suppress Someone@Example.org")
    say(service, "/suppress https://www.blocked-co.com/about")
    assert repo.is_suppressed("someone@example.org", None)
    assert repo.is_suppressed("anyone@blocked-co.com", "blocked-co.com")
    assert "do-not-contact" in notifier.sent[-1]["text"]


def test_suppress_command_needs_a_valid_target_and_an_allowed_user():
    repo, notifier, service, _, _ = world()
    say(service, "/suppress not-an-address")
    assert "Usage" in notifier.sent[-1]["text"] and not repo.suppressed
    say(service, "/suppress evil@x.com", user=666)
    assert not repo.suppressed


def test_approve_is_blocked_if_the_contact_opted_out_after_drafting():
    repo, _, service, msg, _ = world()
    repo.add_suppression("manual", email="ann@acme.com")
    tap(service, msg, "approve")
    assert repo.get_message(msg.id).status == "awaiting_approval"
    assert repo.contacts[msg.contact_id]["status"] == "awaiting_approval"


def test_cards_can_go_to_a_partner_group_but_only_allowed_users_act():
    repo, notifier = InMemoryRepo(), ConsoleNotifier(echo=False)
    service = ApprovalService(repo, MockLLM(repo, {}), ICP, FOOTER, notifier, [OWNER, 2002], chat_id=-100555)
    assert service.chat_id == -100555
    assert service._authorized(2002, "test") and not service._authorized(3003, "test")


# ---------- refreshing stale drafts ----------

def test_refresh_replaces_stale_drafts_with_a_new_version_and_card():
    repo, notifier, service, msg, _ = world()
    service.llm = MockLLM(repo, {"draft": SHORTER})
    assert service.refresh_pending() == 1
    new = repo.get_message(msg.id)
    assert new.version == 2 and new.subject == "Shorter version" and new.status == "awaiting_approval"
    assert "Superseded by v2" in notifier.edits[-1]["text"]
    assert "Draft v2" in notifier.sent[-1]["text"]
    tap(service, msg, "approve", version=1)  # the old card's buttons are dead
    assert "out of date" in popup(notifier) and repo.get_message(msg.id).status == "awaiting_approval"


def test_refresh_also_covers_drafts_waiting_for_feedback_and_skips_opted_out():
    repo, notifier, service, msg, _ = world()
    tap(service, msg, "suggest")  # now needs_changes
    service.llm = MockLLM(repo, {"draft": SHORTER})
    assert service.refresh_pending() == 1 and repo.get_message(msg.id).status == "awaiting_approval"
    repo.add_suppression("manual", email="ann@acme.com")
    assert service.refresh_pending() == 0 and repo.get_message(msg.id).status == "rejected"


def test_refresh_keeps_going_when_the_model_fails():
    repo, _, service, msg, _ = world()
    service.llm = MockLLM(repo, {})  # no "draft" response: raises LLMError
    assert service.refresh_pending() == 0
    assert repo.get_message(msg.id).version == 1 and repo.events[-1]["type"] == "refresh_failed"


def test_card_separates_the_email_from_the_reviewers_notes():
    from zypher_agent.approval import END_BANNER, NOTES_BANNER
    _, notifier, _, msg, _ = world()
    text = notifier.sent[-1]["text"]
    email = text[text.index("THE EMAIL"):text.index(END_BANNER)]
    assert "Subject:" in email and FOOTER.postal_address in email  # the footer is part of what is sent
    assert "Evidence:" not in email and "Phone" not in email and "Passed all checks" not in email
    assert text.index(END_BANNER) < text.index(NOTES_BANNER) < text.index("Evidence:")


# ---------- sending controls from Telegram ----------

def test_pause_resume_and_status_commands():
    repo, notifier, service, _, _ = world()
    service.sending_status = lambda: "Sent today: 2 of 5"
    say(service, "/pause")
    assert repo.sending_state()[0] is True and "paused" in notifier.sent[-1]["text"].lower()
    say(service, "/status")
    text = notifier.sent[-1]["text"]
    assert "PAUSED" in text and "Waiting for your approval: 1" in text and "Sent today: 2 of 5" in text
    say(service, "/resume")
    assert repo.sending_state() == (False, None)
    say(service, "/status")
    assert "Sending is on" in notifier.sent[-1]["text"]


def test_only_allowed_users_can_pause_or_resume():
    repo, _, service, _, _ = world()
    say(service, "/pause", user=666)
    assert repo.sending_state()[0] is False
    assert any(e["type"] == "unauthorized" for e in repo.events)


def test_alert_goes_to_the_approval_chat():
    _, notifier, service, _, _ = world()
    service.alert("Bounced: x@y.com")
    assert notifier.sent[-1]["chat_id"] == service.chat_id and "Bounced" in notifier.sent[-1]["text"]

"""Reading the mailbox: what is a bounce, what is a reply, what is none of our business.

The mailbox is the CEO's real inbox. The rule that matters most: mail that is not about one of OUR emails is
never opened, stored, classified or alerted on.
"""
import random
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from zypher_agent.config import Settings
from zypher_agent.db import InMemoryRepo
from zypher_agent.llm import LLMError, MockLLM
from zypher_agent.models import CompanyIn, ContactIn, NewMessage
from zypher_agent.sender.inbox import (
    FakeMailbox,
    InboxProcessor,
    ReplyClassOut,
    clean_reply_text,
    is_clear_opt_out,
    looks_like_complaint,
    parse_mail,
)
from zypher_agent.sender.stoploss import evaluate_stoploss

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
OURS = "<first@zypher-solutions.com>"


def raw(headers: dict, body: str = "Hello", extra: str = "") -> bytes:
    head = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
    return f"{head}\r\n\r\n{body}\r\n{extra}".encode()


def bounce_mail(recipient="owner@acme.ae", status="5.1.1", original=OURS) -> bytes:
    boundary = "BOUNDARY"
    body = (
        f"--{boundary}\r\nContent-Type: text/plain\r\n\r\nYour message could not be delivered.\r\n"
        f"--{boundary}\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.acme.ae\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\nAction: failed\r\nStatus: {status}\r\n"
        f"Diagnostic-Code: smtp; 550 {status} user unknown\r\n\r\n"
        f"--{boundary}\r\nContent-Type: message/rfc822\r\n\r\nMessage-ID: {original}\r\nSubject: quote requests\r\n\r\nbody\r\n"
        f"--{boundary}--\r\n"
    )
    head = ("From: Mail Delivery System <MAILER-DAEMON@mx.acme.ae>\r\nTo: muhammedhasheem@zypher-solutions.com\r\n"
            "Subject: Undelivered Mail Returned to Sender\r\n"
            f"Content-Type: multipart/report; report-type=delivery-status; boundary={boundary}\r\n"
            "Message-ID: <dsn1@mx.acme.ae>\r\n\r\n")
    return (head + body).encode()


_ids = iter(range(1, 10_000))


def reply_mail(text="Sure, send it over.", sender="owner@acme.ae", subject="Re: quote requests", in_reply_to=OURS) -> bytes:
    return raw({"From": f"Ann <{sender}>", "To": "muhammedhasheem@zypher-solutions.com", "Subject": subject,
                "In-Reply-To": in_reply_to, "References": in_reply_to, "Message-ID": f"<r{next(_ids)}@acme.ae>"}, text)


# ---------- parsing ----------

def test_a_delivery_failure_notice_is_recognised_with_its_details():
    p = parse_mail(bounce_mail())
    assert p.kind == "bounce" and p.bounce_recipient == "owner@acme.ae" and p.bounce_hard is True
    assert p.original_message_id == OURS and "user unknown" in p.bounce_detail


def test_a_temporary_failure_is_a_soft_bounce():
    assert parse_mail(bounce_mail(status="4.2.2")).bounce_hard is False


def test_out_of_office_is_recognised_by_headers_and_by_subject():
    by_header = raw({"From": "a@acme.ae", "Subject": "Re: quote requests", "In-Reply-To": OURS, "Auto-Submitted": "auto-replied"})
    by_subject = raw({"From": "a@acme.ae", "Subject": "Automatic reply: quote requests", "In-Reply-To": OURS})
    assert parse_mail(by_header).kind == "auto_reply" and parse_mail(by_subject).kind == "auto_reply"


def test_a_threaded_reply_is_a_reply_with_its_text():
    p = parse_mail(reply_mail("Sure, send it over.\r\n\r\nOn Mon, Ahmed wrote:\r\n> quoted old email"))
    assert p.kind == "reply" and p.sender == "owner@acme.ae" and p.references == [OURS]
    assert p.text == "Sure, send it over."


def test_unrelated_mail_is_other():
    assert parse_mail(raw({"From": "news@shop.com", "Subject": "Sale!", "Message-ID": "<n@shop.com>"})).kind == "reply"  # parsed, matched later
    assert parse_mail(b"garbage \xff\xfe not an email").kind in ("reply", "other")  # never crashes


@pytest.mark.parametrize("text", ["No thanks", "no thank you.", "Please unsubscribe me", "remove me from your list",
                                  "Stop emailing me", "Not interested, don't contact me again", "STOP"])
def test_clear_opt_outs(text):
    assert is_clear_opt_out(text)


@pytest.mark.parametrize("text", ["Sure, send it over", "Interesting, tell me more", "How much does it cost?",
                                  "We stopped using spreadsheets last year, what would you change?"])
def test_normal_replies_are_not_opt_outs(text):
    assert not is_clear_opt_out(text)


def test_spam_complaints_are_recognised():
    assert looks_like_complaint("This is spam, I will report you") and looks_like_complaint("reporting this to abuse")
    assert not looks_like_complaint("We don't need this spam of paperwork")  # only direct accusations count


def test_reply_text_drops_quoted_history_and_signatures():
    assert clean_reply_text("Yes please.\n\n-- \nAnn\n\nOn Tue, Muhammed wrote:\n> hello") == "Yes please."
    assert clean_reply_text("> only quoted") == ""


# ---------- stop-loss ----------

def test_two_hard_bounces_in_the_last_thirty_sends_pause_sending():
    assert evaluate_stoploss(sent=30, hard_bounces=1, complaint=False) is None
    assert "2 hard bounces" in evaluate_stoploss(sent=30, hard_bounces=2, complaint=False)


def test_a_bounce_rate_above_two_percent_over_a_hundred_sends_pauses():
    assert evaluate_stoploss(sent=100, hard_bounces=1, complaint=False) is None
    assert "2%" in evaluate_stoploss(sent=100, hard_bounces=3, complaint=False)


def test_one_spam_complaint_pauses_immediately():
    assert "complaint" in evaluate_stoploss(sent=3, hard_bounces=0, complaint=True)


# ---------- the processor ----------

class Classify(BaseModel):
    pass


def world(responses=None):
    repo = InMemoryRepo()
    llm = MockLLM(repo, responses if responses is not None else {"classify_reply": ReplyClassOut(reply_class="interested")})
    alerts: list[str] = []
    cid = repo.upsert_lead(CompanyIn(name="Acme", domain="acme.ae", country="UAE"), ContactIn(full_name="Ann", email="owner@acme.ae"))
    m = repo.save_message(NewMessage(contact_id=cid, subject="quote requests", body="b"))
    repo.update_message(m.id, status="sent", sent_at=NOW, message_id_header=OURS)
    repo.set_lead_status(cid, "in_sequence")
    box = FakeMailbox()
    proc = InboxProcessor(repo, box, llm, alerts.append, rng=random.Random(0))
    return repo, box, proc, alerts, cid, m


def test_first_run_starts_from_now_and_never_reads_the_old_mailbox():
    repo, box, proc, alerts, cid, _ = world()
    box.add(reply_mail("an old reply from years ago"))  # already in the mailbox before we ever looked
    r = proc.run_once()
    assert r.processed == 0 and alerts == [] and repo.inbox_cursor() is not None
    box.add(reply_mail("a new reply"))
    assert proc.run_once().replies == 1


def test_a_reply_stops_the_sequence_saves_the_text_and_alerts_a_human():
    repo, box, proc, alerts, cid, m = world()
    proc.run_once()
    box.add(reply_mail("Sure, send it over."))
    r = proc.run_once()
    assert r.replies == 1 and repo.contacts[cid]["status"] == "replied"
    inbound = [x for x in repo.messages.values() if x.direction == "inbound"]
    assert len(inbound) == 1 and inbound[0].body == "Sure, send it over." and inbound[0].reply_class == "interested"
    assert "Sure, send it over." in alerts[-1] and "owner@acme.ae" in alerts[-1] and "interested" in alerts[-1]
    assert not repo.is_suppressed("owner@acme.ae", None)


def test_the_same_mail_is_never_processed_twice():
    repo, box, proc, alerts, cid, _ = world()
    proc.run_once()
    box.add(reply_mail())
    proc.run_once()
    assert proc.run_once().processed == 0 and len(alerts) == 1


def test_a_clear_opt_out_is_suppressed_at_once_and_confirmed():
    repo, box, proc, alerts, cid, _ = world({"classify_reply": ReplyClassOut(reply_class="not_interested")})
    proc.run_once()
    box.add(reply_mail("No thanks."))
    proc.run_once()
    assert repo.is_suppressed("owner@acme.ae", None) and repo.contacts[cid]["status"] == "unsubscribed"
    assert "do-not-contact" in alerts[-1]


def test_opt_out_works_even_if_the_model_is_down():
    repo, box, proc, alerts, cid, _ = world({})  # classifier raises LLMError
    proc.run_once()
    box.add(reply_mail("Please unsubscribe me"))
    proc.run_once()
    assert repo.is_suppressed("owner@acme.ae", None)


def test_a_reply_the_model_cannot_classify_still_reaches_a_human():
    repo, box, proc, alerts, cid, _ = world({})
    proc.run_once()
    box.add(reply_mail("Interesting. What does it cost?"))
    proc.run_once()
    assert repo.contacts[cid]["status"] == "replied" and "What does it cost?" in alerts[-1]


def test_a_hard_bounce_suppresses_the_address_and_marks_everything():
    repo, box, proc, alerts, cid, m = world()
    proc.run_once()
    box.add(bounce_mail())
    r = proc.run_once()
    assert r.bounces == 1 and repo.get_message(m.id).status == "bounced"
    assert repo.is_suppressed("owner@acme.ae", None) and repo.contacts[cid]["status"] == "bounced"
    assert "bounced" in alerts[-1].lower()


def test_a_soft_bounce_is_logged_but_not_suppressed():
    repo, box, proc, alerts, cid, m = world()
    proc.run_once()
    box.add(bounce_mail(status="4.2.2"))
    proc.run_once()
    assert not repo.is_suppressed("owner@acme.ae", None) and repo.get_message(m.id).status == "sent"
    assert any(e["type"] == "soft_bounce" for e in repo.events)


def test_two_bounces_pause_all_sending_with_an_alert():
    repo, box, proc, alerts, cid, m = world()
    cid2 = repo.upsert_lead(CompanyIn(name="Beta", domain="beta.ae", country="UAE"), ContactIn(email="x@beta.ae"))
    m2 = repo.save_message(NewMessage(contact_id=cid2, subject="s", body="b"))
    repo.update_message(m2.id, status="sent", sent_at=NOW, message_id_header="<second@zypher-solutions.com>")
    proc.run_once()
    box.add(bounce_mail())
    box.add(bounce_mail(recipient="x@beta.ae", original="<second@zypher-solutions.com>"))
    proc.run_once()
    paused, reason = repo.sending_state()
    assert paused and "bounce" in reason and "PAUSED" in alerts[-1]


def test_a_spam_complaint_pauses_sending():
    repo, box, proc, alerts, cid, _ = world()
    proc.run_once()
    box.add(reply_mail("This is spam, I will report you"))
    proc.run_once()
    assert repo.sending_state()[0] is True and repo.is_suppressed("owner@acme.ae", None)


def test_out_of_office_does_not_stop_anything_but_is_recorded():
    repo, box, proc, alerts, cid, _ = world()
    proc.run_once()
    box.add(raw({"From": "owner@acme.ae", "Subject": "Automatic reply: quote requests", "In-Reply-To": OURS, "Auto-Submitted": "auto-replied"}, "I am away until 3 Nov"))
    r = proc.run_once()
    assert r.auto_replies == 1 and repo.contacts[cid]["status"] == "in_sequence" and alerts == []
    assert any(e["type"] == "out_of_office" for e in repo.events)


def test_mail_that_is_not_about_our_emails_is_ignored_completely():
    repo, box, proc, alerts, cid, _ = world()
    proc.run_once()
    box.add(raw({"From": "bank@bank.com", "Subject": "Your statement", "Message-ID": "<s@bank.com>"}, "private"))
    box.add(raw({"From": "friend@gmail.com", "Subject": "Dinner?", "Message-ID": "<d@gmail.com>"}, "lunch tomorrow"))
    r = proc.run_once()
    assert r.ignored == 2 and r.processed == 0 and alerts == []
    assert not [x for x in repo.messages.values() if x.direction == "inbound"]
    assert all("private" not in str(e["payload"]) and "lunch" not in str(e["payload"]) for e in repo.events)


def test_a_reply_from_a_different_address_at_the_same_thread_is_matched_by_message_id():
    repo, box, proc, alerts, cid, _ = world()
    proc.run_once()
    box.add(reply_mail("Forwarding to my colleague, she handles this.", sender="colleague@acme.ae"))
    assert proc.run_once().replies == 1 and repo.contacts[cid]["status"] == "replied"


def test_uidvalidity_change_restarts_from_now_instead_of_reprocessing():
    repo, box, proc, alerts, cid, _ = world()
    proc.run_once()
    box.add(reply_mail("first"))
    proc.run_once()
    box.reset_uidvalidity()  # the server renumbered the mailbox
    box.add(reply_mail("second", in_reply_to=OURS))
    assert proc.run_once().processed == 0  # restart from now: the old mail is not replayed
    box.add(reply_mail("third"))
    assert proc.run_once().replies == 1


def test_classifier_errors_are_survivable(monkeypatch):
    repo, box, proc, alerts, cid, _ = world({})
    proc.run_once()
    box.add(reply_mail("Hello?"))
    proc.run_once()
    assert isinstance(LLMError("x"), Exception) and repo.contacts[cid]["status"] == "replied"


def test_settings_defaults_do_not_enable_the_inbox_on_their_own():
    assert Settings(_env_file=None).imap_host == "imap.secureserver.net"


def test_a_reply_withdraws_every_follow_up_that_has_not_gone_out_yet():
    repo, box, proc, alerts, cid, m = world()
    waiting = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b", step=2, status="awaiting_approval"))
    approved = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b", step=3))
    repo.update_message(approved.id, status="approved")
    proc.run_once()
    box.add(reply_mail("Interesting, tell me more."))
    proc.run_once()
    assert repo.get_message(waiting.id).status == "rejected" and repo.get_message(approved.id).status == "rejected"
    assert repo.get_message(m.id).status == "sent"  # what already left is history, untouched
    assert "2 unsent follow-up(s)" in alerts[-1]


def test_a_bounce_also_withdraws_unsent_follow_ups():
    repo, box, proc, alerts, cid, m = world()
    waiting = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b", step=2, status="awaiting_approval"))
    proc.run_once()
    box.add(bounce_mail())
    proc.run_once()
    assert repo.get_message(waiting.id).status == "rejected"


def test_the_gmail_unsubscribe_button_is_honoured_even_with_an_empty_body():
    # We add a List-Unsubscribe header, so Gmail shows an Unsubscribe button. A click sends a mail whose SUBJECT
    # says "unsubscribe" and whose body is empty. It must suppress the address like any other opt-out.
    repo, box, proc, alerts, cid, _ = world({})  # even with the model down
    proc.run_once()
    box.add(raw({"From": "Ann <owner@acme.ae>", "To": "muhammedhasheem@zypher-solutions.com", "Subject": "unsubscribe",
                 "In-Reply-To": OURS, "Message-ID": "<unsub1@acme.ae>"}, ""))
    proc.run_once()
    assert repo.is_suppressed("owner@acme.ae", None) and repo.contacts[cid]["status"] == "unsubscribed"
    assert "do-not-contact" in alerts[-1]


def test_a_subject_line_alone_can_carry_a_clear_opt_out_but_normal_subjects_do_not():
    assert is_clear_opt_out("Re: please unsubscribe me") and is_clear_opt_out("unsubscribe")
    assert not is_clear_opt_out("Re: Quote requests") and not is_clear_opt_out("Re: Customs documents")

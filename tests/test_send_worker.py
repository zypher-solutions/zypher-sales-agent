"""The worker end to end, with a fake mail server. Every way an email can fail to leave, or leave twice."""
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_guardrails import FOOTER, GOOD_BODY, HYP

from zypher_agent.config import Settings, load_icp
from zypher_agent.db import InMemoryRepo
from zypher_agent.drafting.email import DraftResult, evidence_for, render_email
from zypher_agent.guardrails.checks import check_draft
from zypher_agent.models import (
    Claim,
    CompanyIn,
    ContactIn,
    EmailDraft,
    NewMessage,
    ResearchReport,
)
from zypher_agent.sender.transport import (
    AmbiguousSendError,
    FakeTransport,
    PermanentSendError,
    SendConfigError,
    TransientSendError,
)
from zypher_agent.sender.worker import SendWorker, allowlisted

ICP = load_icp(Settings(_env_file=None).icp_path)
DUBAI = ZoneInfo("Asia/Dubai")
TUE_10AM = datetime(2026, 10, 20, 10, 0, tzinfo=DUBAI).astimezone(UTC)  # inside the UAE window
GOOD = EmailDraft(subject="quote requests", body=GOOD_BODY, claims=[Claim(text="q", evidence_id="h1")])
REPORT = ResearchReport(hypotheses=[HYP], summary="A freight company.")


class World:
    def __init__(self, **settings):
        self.repo, self.transport, self.alerts = InMemoryRepo(), FakeTransport(), []
        base = {"_env_file": None, "sending_enabled": True, "sender_email": FOOTER.sender_email}
        self.settings = Settings(**{**base, **settings})
        self.worker = SendWorker(self.repo, self.transport, self.settings, FOOTER, ICP, alert=self.alerts.append, rng=random.Random(1))

    def lead(self, name="Acme", domain="acme.ae", email="owner@acme.ae", country="UAE") -> str:
        return self.repo.upsert_lead(CompanyIn(name=name, domain=domain, country=country), ContactIn(full_name="Ann Lee", email=email))

    def message(self, cid: str, approve: bool = True, step: int = 1, draft: EmailDraft = GOOD):
        lead = self.repo.get_lead(cid)
        self.repo.save_research(lead.company_id, REPORT)
        check = check_draft(draft, REPORT.hypotheses, ICP, FOOTER)
        result = DraftResult(draft, check, 1, render_email(draft, "Ann Lee", FOOTER))
        msg = self.repo.save_message(NewMessage(
            contact_id=cid, subject=draft.subject, body=result.text, evidence=evidence_for(result, REPORT), step=step))
        if approve:
            self.repo.update_message(msg.id, status="approved")
            self.repo.log_event("approved", contact_id=cid, message_id=msg.id, actor="human:1", payload={"version": 1})
        return self.repo.get_message(msg.id)

    def run(self, now=TUE_10AM):
        return self.worker.run_once(now)

    def state(self, msg_id):
        return self.repo.get_message(msg_id)


def test_an_approved_email_is_sent_once_and_recorded():
    w = World()
    msg = w.message(w.lead())
    s = w.run()
    assert s.sent == 1 and len(w.transport.sent) == 1
    out, saved = w.transport.sent[0], w.state(msg.id)
    assert out.to == "owner@acme.ae" and out.subject == "quote requests" and out.from_addr == FOOTER.sender_email
    assert saved.status == "sent" and saved.sent_at == TUE_10AM and saved.message_id_header == out.message_id
    assert w.repo.contacts[msg.contact_id]["status"] == "in_sequence"
    assert [e["type"] for e in w.repo.events if e["type"] == "sent"] == ["sent"]
    w.run(TUE_10AM + timedelta(hours=1))  # a later pass finds nothing left to send
    assert len(w.transport.sent) == 1


def test_master_switch_off_sends_nothing_even_if_everything_is_approved():
    w = World(sending_enabled=False)
    w.message(w.lead())
    s = w.run()
    assert s.disabled and w.transport.sent == []


def test_unapproved_messages_never_go():
    w = World()
    w.message(w.lead(), approve=False)
    assert w.run().sent == 0 and w.transport.sent == []


def test_paused_tenant_sends_nothing():
    w = World()
    w.message(w.lead())
    w.repo.set_sending_paused(True, "bounce spike")
    s = w.run()
    assert s.paused == "bounce spike" and w.transport.sent == []
    w.repo.set_sending_paused(False)
    assert w.run().sent == 1


def test_text_changed_after_approval_is_not_sent():
    w = World()
    msg = w.message(w.lead())
    w.repo.update_message(msg.id, version=2)  # someone redrafted: approval was for v1
    s = w.run()
    assert s.sent == 0 and any("not approved" in b for b in s.blocked)


def test_rules_tightened_after_approval_still_block():
    w = World()
    bad = GOOD.model_copy(update={"body": GOOD_BODY + " Our pricing is low."})  # stored evidence says it passed before
    msg = w.message(w.lead(), draft=bad)
    s = w.run()
    assert s.sent == 0 and "pricing" in " ".join(s.blocked) and w.state(msg.id).status == "approved"


def test_opting_out_after_approval_cancels_the_email():
    w = World()
    msg = w.message(w.lead())
    w.repo.add_suppression("manual", email="owner@acme.ae")
    w.run()
    assert w.transport.sent == [] and w.state(msg.id).status == "rejected"
    assert w.repo.contacts[msg.contact_id]["status"] == "unsubscribed"


def test_outside_business_hours_the_message_waits_for_the_window():
    w = World()
    msg = w.message(w.lead())
    sat = datetime(2026, 10, 24, 10, 0, tzinfo=DUBAI).astimezone(UTC)
    s = w.run(sat)
    assert s.deferred == 1 and w.transport.sent == []
    assert w.state(msg.id).send_after == datetime(2026, 10, 26, 9, 0, tzinfo=DUBAI).astimezone(UTC)
    assert w.run(sat + timedelta(hours=1)).deferred == 0  # not due yet: not even looked at again


def test_daily_cap_and_spacing_hold_back_the_rest():
    w = World(daily_send_cap=2)
    for i in range(3):
        w.message(w.lead(name=f"Co{i}", domain=f"co{i}.ae", email=f"o@co{i}.ae"))
    assert w.run().sent == 1                                  # one now; the others must wait for the spacing gap
    assert w.run(TUE_10AM + timedelta(minutes=13)).sent == 1  # gap passed: second goes
    assert w.run(TUE_10AM + timedelta(minutes=30)).sent == 0  # cap of 2 reached
    assert len(w.transport.sent) == 2


def test_allowlist_limits_who_can_receive_during_the_rollout():
    w = World(send_allowlist="me@gmail.com, test-domain.com")
    w.message(w.lead())  # owner@acme.ae is not on the list
    assert w.run().sent == 0 and w.transport.sent == []
    assert allowlisted("a@Test-Domain.com", "me@gmail.com, test-domain.com") and allowlisted("x@y.z", "")
    assert not allowlisted("x@evil-test-domain.com", "test-domain.com")


# ---------- failures ----------

def failing_world(error):
    w = World()
    w.transport = FakeTransport(fail_with={"owner@acme.ae": error})
    w.worker.transport = w.transport
    return w, w.message(w.lead())


def test_refused_address_is_a_bounce_and_is_never_tried_again():
    w, msg = failing_world(PermanentSendError("550 no such user"))
    s = w.run()
    assert w.state(msg.id).status == "bounced" and w.repo.is_suppressed("owner@acme.ae", None)
    assert w.repo.contacts[msg.contact_id]["status"] == "bounced" and s.sent == 0 and w.alerts


def test_temporary_failure_retries_later_then_gives_up():
    w, msg = failing_world(TransientSendError("451 try later"))
    w.run()
    first = w.state(msg.id)
    assert first.status == "approved" and first.send_attempts == 1 and first.send_after > TUE_10AM
    w.run(first.send_after)
    w.run(w.state(msg.id).send_after)
    assert w.state(msg.id).status == "failed" and "gave up" in w.state(msg.id).last_error


def test_a_connection_lost_mid_send_is_never_retried_automatically():
    w, msg = failing_world(AmbiguousSendError("connection lost"))
    w.run()
    assert w.state(msg.id).status == "failed"
    w.run(TUE_10AM + timedelta(days=1))
    assert w.transport.sent == [] and "NOT be retried" in w.alerts[-1] and "Sent folder" in w.alerts[-1]


def test_wrong_mailbox_login_pauses_everything_and_alerts():
    w, msg = failing_world(SendConfigError("login refused"))
    s = w.run()
    assert w.repo.sending_state()[0] is True and "login" in (w.repo.sending_state()[1] or "") and "/resume" in w.alerts[-1]
    assert s.paused and w.state(msg.id).status == "approved"  # handed back, not stuck in the queue
    w.transport.fail_with.clear()  # the owner fixes the password and resumes
    w.repo.set_sending_paused(False)
    assert w.run(TUE_10AM + timedelta(minutes=1)).sent == 1 and w.state(msg.id).status == "sent"


def test_two_workers_cannot_claim_the_same_message():
    w = World()
    msg = w.message(w.lead())
    assert w.repo.claim_for_sending(msg.id, "<a@x>", "me@x.com") is True
    assert w.repo.claim_for_sending(msg.id, "<b@x>", "me@x.com") is False


# ---------- one first email per company, follow-ups thread ----------

def test_only_one_contact_per_company_gets_a_first_email():
    w = World()
    a = w.message(w.lead(email="a@acme.ae"))
    b = w.message(w.lead(email="b@acme.ae"))
    w.run()
    s = w.run(TUE_10AM + timedelta(minutes=15))
    assert len(w.transport.sent) == 1 and any("already received" in x for x in s.blocked)
    assert {w.state(a.id).status, w.state(b.id).status} == {"sent", "approved"}


def test_a_follow_up_is_threaded_under_the_first_email():
    w = World()
    cid = w.lead()
    first = w.message(cid)
    w.run()
    sent_first = w.transport.sent[0]
    follow = w.message(cid, step=2)
    w.run(TUE_10AM + timedelta(days=3, minutes=1))
    out = w.transport.sent[1]
    assert out.in_reply_to == sent_first.message_id and out.subject == "Re: quote requests"
    assert w.state(follow.id).in_reply_to == sent_first.message_id and w.state(first.id).status == "sent"


@pytest.mark.parametrize("country", [None, "Atlantis"])
def test_unknown_country_blocks_instead_of_guessing(country):
    w = World()
    w.message(w.lead(country=country))
    s = w.run()
    assert s.sent == 0 and any("country" in b for b in s.blocked)



"""Follow-ups: when they are written, when they must NOT be, and that a sequence can never restart or run on."""
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from zypher_agent.config import Settings, kind_for_step, load_icp
from zypher_agent.dry_run import FOLLOWUPS, build, press
from zypher_agent.models import EmailDraft
from zypher_agent.sender.transport import FakeTransport
from zypher_agent.sender.worker import SendWorker

DUBAI = ZoneInfo("Asia/Dubai")
SENT_AT = datetime(2026, 10, 20, 10, 0, tzinfo=DUBAI).astimezone(UTC)  # Tuesday
ICP = load_icp(Settings(_env_file=None).icp_path)


def at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=DUBAI).astimezone(UTC)


def world():
    """Acme's first email has been approved and sent on Tuesday 20 Oct; nobody has replied."""
    run = build(echo=False)
    run.pipeline.run(10)
    msg = next(m for m in run.repo.messages.values() if m.step == 1 and "acme" in run.repo.get_lead(m.contact_id).domain)
    run.repo.update_message(msg.id, status="sent", sent_at=SENT_AT, message_id_header="<first@zypher.test>")
    run.repo.set_lead_status(msg.contact_id, "in_sequence")
    return run, msg.contact_id


def steps(run, cid):
    return sorted(m.step for m in run.repo.messages.values() if m.contact_id == cid and m.direction == "outbound")


def test_nothing_is_written_before_the_wait_is_over():
    run, cid = world()
    run.pipeline.run_followups(now=at(22))  # Thursday: only 2 working days
    assert steps(run, cid) == [1]


def test_after_three_working_days_email_two_goes_to_you_for_approval():
    run, cid = world()
    s = run.pipeline.run_followups(now=at(23))  # Friday: 3 working days in the UAE
    assert s.followups == 1 and steps(run, cid) == [1, 2]
    two = next(m for m in run.repo.messages.values() if m.step == 2)
    assert two.status == "awaiting_approval" and run.repo.contacts[cid]["status"] == "awaiting_approval"
    assert "Evidence:" in run.notifier.sent[-1]["text"] and "Draft v1" in run.notifier.sent[-1]["text"]
    assert two.evidence["guardrails"]["violations"] == []


def test_a_follow_up_is_written_only_once_while_it_waits_for_you():
    run, cid = world()
    run.pipeline.run_followups(now=at(23))
    run.pipeline.run_followups(now=at(24))
    assert steps(run, cid) == [1, 2]


def test_the_follow_up_prompt_shows_what_was_already_sent_and_asks_for_something_new():
    run, cid = world()
    run.pipeline.run_followups(now=at(23))
    call = [c for c in run.llm.calls if c["task"] == "followup"][0]
    assert "<emails_already_sent>" in call["user"] and "Email 1 (sent)" in call["user"] and "email 2 now" in call["user"]
    assert "A DIFFERENT observation" in call["system"] and "following up" in call["system"]  # told what NOT to say
    assert "untrusted" in call["system"].lower()


def test_rejecting_a_follow_up_never_restarts_the_sequence_at_step_one():
    run, cid = world()
    run.pipeline.run_followups(now=at(23))
    press(run, "Acme Freight", "reject")
    assert run.repo.contacts[cid]["status"] == "in_sequence"  # NOT 'researched'
    run.pipeline.run(10)  # the normal run must not write a second first email
    assert steps(run, cid) == [1, 2]
    run.pipeline.run_followups(now=at(24))
    assert steps(run, cid) == [1, 2, 2]  # a fresh step 2, not a step 1


def test_the_pipeline_never_drafts_a_first_email_for_someone_already_emailed():
    run, cid = world()
    run.repo.set_lead_status(cid, "researched")  # whatever put it back
    run.pipeline.run(10)
    assert steps(run, cid) == [1] and run.repo.contacts[cid]["status"] != "awaiting_approval"


def test_no_follow_up_after_a_reply_a_bounce_or_an_opt_out():
    for status in ("replied", "bounced", "unsubscribed", "disqualified"):
        run, cid = world()
        run.repo.set_lead_status(cid, status)
        run.pipeline.run_followups(now=at(30))
        assert steps(run, cid) == [1], status


def test_an_opt_out_after_sending_stops_the_sequence_even_if_status_is_stale():
    run, cid = world()
    run.repo.add_suppression("unsubscribe", email="rahul@acme-freight-demo.com")
    run.pipeline.run_followups(now=at(30))
    assert steps(run, cid) == [1] and run.repo.contacts[cid]["status"] == "unsubscribed"


def test_an_out_of_office_notice_holds_the_follow_up_for_a_week():
    run, cid = world()
    run.repo.log_event("out_of_office", contact_id=cid, payload={"from": "x"})  # logged "now"
    run.pipeline.run_followups(now=datetime.now(UTC) + timedelta(days=2))
    assert steps(run, cid) == [1]
    run.pipeline.run_followups(now=datetime.now(UTC) + timedelta(days=9))
    assert steps(run, cid) == [1, 2]


def test_a_lead_with_an_unknown_country_gets_no_follow_up_instead_of_a_guessed_clock():
    run, cid = world()
    co = run.repo.companies[run.repo.contacts[cid]["company_id"]]
    co["country"] = "Atlantis"
    run.pipeline.run_followups(now=at(30))
    assert steps(run, cid) == [1]


def test_the_whole_sequence_runs_in_order_then_stops_for_good():
    run, cid = world()
    worker = SendWorker(run.repo, FakeTransport(), Settings(_env_file=None, sending_enabled=True), run.pipeline.footer,
                        ICP, rng=random.Random(0))
    now = SENT_AT
    for days in (7, 7, 14):
        now += timedelta(days=days)
        run.pipeline.run_followups(now=now)
        two = [m for m in run.repo.messages.values() if m.contact_id == cid and m.status == "awaiting_approval"]
        assert len(two) == 1
        run.repo.update_message(two[0].id, status="approved")
        run.repo.log_event("approved", contact_id=cid, message_id=two[0].id, actor="human:1", payload={"version": 1})
        assert worker.run_once(now).sent == 1
    assert steps(run, cid) == [1, 2, 3, 4]
    s = run.pipeline.run_followups(now=now + timedelta(days=30))
    assert s.completed == 1 and run.repo.contacts[cid]["status"] == "nurture"
    run.pipeline.run_followups(now=now + timedelta(days=60))
    assert steps(run, cid) == [1, 2, 3, 4]  # nothing after the last email


def test_follow_ups_are_sent_in_the_same_thread_as_the_first_email():
    run, cid = world()
    transport = FakeTransport()
    worker = SendWorker(run.repo, transport, Settings(_env_file=None, sending_enabled=True), run.pipeline.footer, ICP, rng=random.Random(0))
    run.pipeline.run_followups(now=at(27))
    two = next(m for m in run.repo.messages.values() if m.step == 2)
    run.repo.update_message(two.id, status="approved")
    run.repo.log_event("approved", contact_id=cid, message_id=two.id, actor="human:1", payload={"version": 1})
    assert worker.run_once(at(27)).sent == 1
    out = transport.sent[0]
    assert out.in_reply_to == "<first@zypher.test>" and out.subject.startswith("Re: ")


def test_a_reply_withdraws_follow_ups_that_have_not_gone_yet_and_the_worker_would_not_send_them_anyway():
    run, cid = world()
    run.pipeline.run_followups(now=at(23))
    two = next(m for m in run.repo.messages.values() if m.step == 2)
    run.repo.update_message(two.id, status="approved")
    run.repo.log_event("approved", contact_id=cid, message_id=two.id, actor="human:1", payload={"version": 1})
    run.repo.set_lead_status(cid, "replied")  # the reply arrives after approval, before sending
    transport = FakeTransport()
    worker = SendWorker(run.repo, transport, Settings(_env_file=None, sending_enabled=True), run.pipeline.footer, ICP, rng=random.Random(0))
    s = worker.run_once(at(23))
    assert transport.sent == [] and any("sequence is over" in b for b in s.blocked)
    assert run.repo.cancel_unsent(cid, "contact replied") == 1 and run.repo.get_message(two.id).status == "rejected"


def test_every_step_in_the_config_has_a_kind_with_rules_and_a_canned_dry_run_draft():
    assert [s.step for s in ICP.sequence] == [2, 3, 4]
    assert [kind_for_step(ICP, n) for n in (1, 2, 3, 4)] == ["first_touch", "new_angle", "referral", "breakup"]
    assert set(FOLLOWUPS) == {2, 3, 4} and all(isinstance(d, EmailDraft) for d in FOLLOWUPS.values())

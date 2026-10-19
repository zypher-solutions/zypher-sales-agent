"""The numbers must be right, because they decide whether you change the offer."""
from datetime import UTC, datetime, timedelta

from zypher_agent.db import InMemoryRepo
from zypher_agent.models import CompanyIn, ContactIn, NewMessage
from zypher_agent.sender.stats import (
    build_report,
    check_reply_rate,
    format_report,
    low_reply_rate_warning,
    reading_guide,
)

NOW = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)


def emailed(repo, i, days_ago=1, step=1):
    cid = repo.upsert_lead(CompanyIn(name=f"Co{i}", domain=f"co{i}.ae"), ContactIn(email=f"o@co{i}.ae"))
    m = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b", step=step))
    repo.update_message(m.id, status="sent", sent_at=NOW - timedelta(days=days_ago))
    return cid


def stamp(repo, days_ago):
    """Backdate the event just logged (in memory events carry their own timestamp)."""
    repo.events[-1]["at"] = NOW - timedelta(days=days_ago)


def replied(repo, cid, cls="interested", days_ago=1, opt_out=False, complaint=False):
    repo.log_event("replied", contact_id=cid, payload={"class": cls, "opt_out": opt_out, "complaint": complaint})
    stamp(repo, days_ago)


def test_an_empty_database_reports_zeros_and_no_division_errors():
    p7, pall = build_report(InMemoryRepo(), NOW)
    assert (pall.first_emails, pall.replied, pall.reply_rate, pall.bounce_rate) == (0, 0, None, None)
    assert "n/a" in format_report([p7, pall]) and "Too early" in format_report([p7, pall])


def test_counts_first_emails_follow_ups_and_companies_separately():
    repo = InMemoryRepo()
    a, _ = emailed(repo, 1), emailed(repo, 2)
    m = repo.save_message(NewMessage(contact_id=a, subject="s", body="b", step=2))
    repo.update_message(m.id, status="sent", sent_at=NOW - timedelta(hours=2))
    _, p = build_report(repo, NOW)
    assert (p.first_emails, p.follow_ups, p.companies_emailed) == (2, 1, 2)  # two companies, three emails


def test_reply_rate_counts_each_person_once_with_the_right_denominator():
    repo = InMemoryRepo()
    ids = [emailed(repo, i) for i in range(10)]
    replied(repo, ids[0], "interested")
    replied(repo, ids[0], "question")      # the same person replying twice is still one reply
    replied(repo, ids[1], "question")
    replied(repo, ids[2], "not_interested", opt_out=True)
    replied(repo, ids[3], "other")
    _, p = build_report(repo, NOW)
    assert p.replied == 4 and p.reply_rate == 0.4 and p.positive == 2 and p.opt_outs == 1
    assert p.by_class == {"interested": 1, "question": 1, "not_interested": 1, "other": 1}


def test_complaints_and_bounces_are_counted():
    repo = InMemoryRepo()
    ids = [emailed(repo, i) for i in range(5)]
    replied(repo, ids[0], "other", opt_out=True, complaint=True)
    repo.log_event("bounced", contact_id=ids[1], payload={})
    stamp(repo, 1)
    repo.log_event("bounced", contact_id=ids[1], payload={})  # the same address twice counts once
    stamp(repo, 1)
    _, p = build_report(repo, NOW)
    assert (p.complaints, p.bounced, p.bounce_rate) == (1, 1, 1 / 5)


def test_the_seven_day_view_leaves_out_older_activity():
    repo = InMemoryRepo()
    old, new = emailed(repo, 1, days_ago=20), emailed(repo, 2, days_ago=2)
    replied(repo, old, days_ago=19)
    replied(repo, new, days_ago=1)
    p7, pall = build_report(repo, NOW)
    assert (p7.first_emails, p7.replied) == (1, 1) and (pall.first_emails, pall.replied) == (2, 2)


def test_the_reading_guide_is_honest_about_small_numbers_and_bad_signs():
    repo = InMemoryRepo()
    ids = [emailed(repo, i) for i in range(60)]
    _, p = build_report(repo, NOW)
    assert "Too early" in reading_guide(build_report(InMemoryRepo(), NOW)[1])
    assert "normal range" in reading_guide(p) or "strong" in reading_guide(p) or "under 1%" in reading_guide(p)
    for cid in ids[:4]:
        repo.log_event("bounced", contact_id=cid, payload={}); stamp(repo, 1)
    assert "Bounce rate is above 2%" in reading_guide(build_report(repo, NOW)[1])


# ---------- the low reply-rate rule ----------

def test_no_warning_before_300_companies_even_at_zero_replies():
    assert low_reply_rate_warning(299, 0) is None


def test_warning_after_300_companies_below_one_percent():
    msg = low_reply_rate_warning(300, 2)
    assert msg and "0.7%" in msg and "offer or the targeting" in msg


def test_no_warning_at_exactly_one_percent_or_more():
    assert low_reply_rate_warning(300, 3) is None and low_reply_rate_warning(1000, 40) is None


def test_the_alert_goes_out_once_then_waits_for_100_more_emails():
    repo, alerts = InMemoryRepo(), []
    for i in range(300):
        emailed(repo, i)
    assert check_reply_rate(repo, alerts.append) is True and len(alerts) == 1 and "300" in alerts[0]
    assert check_reply_rate(repo, alerts.append) is False and len(alerts) == 1      # same numbers: quiet
    for i in range(300, 399):
        emailed(repo, i)
    assert check_reply_rate(repo, alerts.append) is False                            # +99: still quiet
    emailed(repo, 400)
    assert check_reply_rate(repo, alerts.append) is True and len(alerts) == 2         # +100: reminded


def test_the_alert_never_pauses_sending():
    repo = InMemoryRepo()
    for i in range(300):
        emailed(repo, i)
    check_reply_rate(repo, lambda text: None)
    assert repo.sending_state() == (False, None)

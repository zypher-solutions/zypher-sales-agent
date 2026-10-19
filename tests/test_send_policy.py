"""The send policy is the last gate before an email leaves. Every rule has a test that proves it blocks or defers."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from zypher_agent.sender.policy import (
    HARD_CEILING,
    Decision,
    SendContext,
    daily_cap,
    decide,
    local_window_start,
    normalize_country,
)

DUBAI, DOHA, KOLKATA, NYC = (ZoneInfo(z) for z in ("Asia/Dubai", "Asia/Qatar", "Asia/Kolkata", "America/New_York"))


def at(tz, y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(UTC)


# 2026-10-20 is a Tuesday. 2026-10-23 Fri, 24 Sat, 25 Sun, 26 Mon.
TUE_10AM_DUBAI = at(DUBAI, 2026, 10, 20, 10)


def ctx(**kw) -> SendContext:
    base = dict(
        now=TUE_10AM_DUBAI, country="UAE", status="approved", version=2, approved_version=2,
        paused=False, suppressed=False, violations=[], step=1, company_already_contacted=False,
        sent_today=0, first_sent_at=None, last_sent_at=None, min_gap_seconds=300, env_cap=None,
        sender_tz=KOLKATA,
    )
    return SendContext(**{**base, **kw})


def kind(c: SendContext) -> str:
    return decide(c).kind


def test_a_clean_approved_message_in_the_window_is_allowed():
    assert decide(ctx()) == Decision("allow", "ok", None)


# ---------- approval ----------

@pytest.mark.parametrize("status", ["draft", "awaiting_approval", "needs_changes", "rejected", "sent", "queued", "bounced"])
def test_only_approved_messages_can_go(status):
    d = decide(ctx(status=status))
    assert d.kind == "block" and "approved" in d.reason


def test_approval_must_be_for_this_exact_version():
    assert kind(ctx(approved_version=1)) == "block"  # you approved v1, the text is now v2
    assert kind(ctx(approved_version=None)) == "block"


# ---------- stop switches ----------

def test_paused_sending_blocks_everything():
    d = decide(ctx(paused=True))
    assert d.kind == "block" and "paused" in d.reason


def test_suppressed_contact_is_blocked():
    assert decide(ctx(suppressed=True)).kind == "block"


def test_failed_guardrails_block_even_after_approval():
    d = decide(ctx(violations=["banned_phrase", "links"]))
    assert d.kind == "block" and "banned_phrase" in d.reason


def test_a_first_email_is_blocked_if_the_company_was_already_contacted():
    assert kind(ctx(company_already_contacted=True)) == "block"
    assert kind(ctx(company_already_contacted=True, step=2)) == "allow"  # follow-ups are the same contact


def test_blocks_win_over_defers():
    # outside hours AND paused: the human must know it is paused, not just "wait until Monday"
    assert kind(ctx(paused=True, now=at(DUBAI, 2026, 10, 24, 10))) == "block"


# ---------- send windows in the lead's local time ----------

@pytest.mark.parametrize(("when", "ok"), [
    (at(DUBAI, 2026, 10, 20, 8, 59), False), (at(DUBAI, 2026, 10, 20, 9, 0), True),
    (at(DUBAI, 2026, 10, 20, 11, 29), True), (at(DUBAI, 2026, 10, 20, 11, 30), False),
    (at(DUBAI, 2026, 10, 20, 12, 59), False), (at(DUBAI, 2026, 10, 20, 13, 0), True),
    (at(DUBAI, 2026, 10, 20, 15, 59), True), (at(DUBAI, 2026, 10, 20, 16, 0), False),
])
def test_window_edges_in_uae(when, ok):
    assert (kind(ctx(now=when)) == "allow") is ok


def test_uae_weekend_is_saturday_and_sunday():
    assert kind(ctx(now=at(DUBAI, 2026, 10, 23, 10))) == "allow"    # Friday
    assert kind(ctx(now=at(DUBAI, 2026, 10, 24, 10))) == "defer"    # Saturday
    assert kind(ctx(now=at(DUBAI, 2026, 10, 25, 10))) == "defer"    # Sunday


def test_qatar_weekend_is_friday_and_saturday():
    assert kind(ctx(country="Qatar", now=at(DOHA, 2026, 10, 25, 10))) == "allow"  # Sunday is a working day
    assert kind(ctx(country="Qatar", now=at(DOHA, 2026, 10, 23, 10))) == "defer"  # Friday


def test_the_leads_clock_decides_not_ours():
    # 10:00 in Dubai is 11:30 in Kolkata (just outside India's window) and 02:00 in New York.
    now = at(DUBAI, 2026, 10, 20, 10)
    assert kind(ctx(now=now, country="UAE")) == "allow"
    assert kind(ctx(now=now, country="India")) == "defer"
    assert kind(ctx(now=now, country="US")) == "defer"


def test_deferred_until_the_next_window_start_in_the_leads_time():
    d = decide(ctx(now=at(DUBAI, 2026, 10, 20, 11, 45)))  # after the morning window
    assert d.kind == "defer" and d.until == at(DUBAI, 2026, 10, 20, 13)
    d = decide(ctx(now=at(DUBAI, 2026, 10, 20, 17)))      # after the afternoon window: tomorrow morning
    assert d.until == at(DUBAI, 2026, 10, 21, 9)
    d = decide(ctx(now=at(DUBAI, 2026, 10, 23, 17)))      # Friday evening: next working day is Monday
    assert d.until == at(DUBAI, 2026, 10, 26, 9)


def test_unknown_country_is_blocked_not_guessed():
    d = decide(ctx(country="Atlantis"))
    assert d.kind == "block" and "country" in d.reason
    assert kind(ctx(country=None)) == "block"


@pytest.mark.parametrize(("raw", "expected"), [
    ("UAE", "UAE"), ("United Arab Emirates", "UAE"), ("ae", "UAE"), (" dubai ", "UAE"),
    ("India", "India"), ("IN", "India"), ("Qatar", "Qatar"), ("QA", "Qatar"),
    ("US", "US"), ("USA", "US"), ("United States", "US"), ("Mars", None), (None, None),
])
def test_country_names_are_normalised(raw, expected):
    assert normalize_country(raw) == expected


# ---------- daily cap and warmup ramp ----------

@pytest.mark.parametrize(("day", "cap"), [(1, 5), (7, 5), (8, 8), (14, 8), (15, 12), (21, 12), (22, 15), (200, 15)])
def test_ramp_by_day_since_first_send(day, cap):
    first = TUE_10AM_DUBAI - timedelta(days=day - 1)
    assert daily_cap(first, TUE_10AM_DUBAI, None, KOLKATA) == cap


def test_cap_before_any_send_is_the_day_one_cap():
    assert daily_cap(None, TUE_10AM_DUBAI, None, KOLKATA) == 5


def test_owner_can_lower_the_cap_but_never_raise_it():
    assert daily_cap(None, TUE_10AM_DUBAI, 2, KOLKATA) == 2
    assert daily_cap(None, TUE_10AM_DUBAI, 50, KOLKATA) == 5          # ramp still applies
    late = TUE_10AM_DUBAI - timedelta(days=100)
    assert daily_cap(late, TUE_10AM_DUBAI, 500, KOLKATA) <= HARD_CEILING == 20


def test_reaching_the_cap_defers_to_the_next_day_of_the_sender():
    assert kind(ctx(sent_today=4)) == "allow"
    d = decide(ctx(sent_today=5))
    assert d.kind == "defer" and "cap" in d.reason
    # next sender-day (Asia/Kolkata) starts at 00:00 IST
    assert d.until == at(KOLKATA, 2026, 10, 21, 0)


def test_follow_ups_count_toward_the_same_cap():
    # the caller counts every sent message today, whatever the step
    assert kind(ctx(sent_today=5, step=3)) == "defer"


# ---------- spacing ----------

def test_sends_are_spaced_out():
    just_sent = TUE_10AM_DUBAI - timedelta(seconds=100)
    d = decide(ctx(last_sent_at=just_sent, min_gap_seconds=300))
    assert d.kind == "defer" and d.until == just_sent + timedelta(seconds=300)
    assert kind(ctx(last_sent_at=TUE_10AM_DUBAI - timedelta(seconds=301), min_gap_seconds=300)) == "allow"


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        decide(replace(ctx(), now=datetime(2026, 10, 20, 10, 0)))


def test_local_window_start_helper():
    assert local_window_start(DUBAI, {0, 1, 2, 3, 4}, at(DUBAI, 2026, 10, 20, 8)) == at(DUBAI, 2026, 10, 20, 9)


# ---------- follow-up timing: working days in the lead's own country ----------

def test_business_days_skip_the_leads_weekend():
    from zypher_agent.sender.policy import business_days_since
    tue = at(DUBAI, 2026, 10, 20, 10)
    assert business_days_since(tue, at(DUBAI, 2026, 10, 23, 9), "UAE") == 3      # Wed, Thu, Fri
    thu = at(DUBAI, 2026, 10, 22, 10)
    assert business_days_since(thu, at(DUBAI, 2026, 10, 26, 9), "UAE") == 2      # Fri, (Sat, Sun skipped), Mon
    assert business_days_since(thu, at(DOHA, 2026, 10, 25, 9), "Qatar") == 1     # Qatar works Sunday, rests Fri-Sat
    assert business_days_since(tue, tue, "UAE") == 0
    assert business_days_since(tue, tue, "Atlantis") is None


def test_the_clock_that_counts_is_the_leads_not_ours():
    from zypher_agent.sender.policy import business_days_since
    # 23:30 Tuesday in Dubai is already Wednesday in Kolkata... no: Kolkata is 1.5h BEHIND Dubai, so still Tuesday there
    sent = at(DUBAI, 2026, 10, 20, 23, 30)
    assert business_days_since(sent, at(DUBAI, 2026, 10, 21, 0, 30), "UAE") == 1      # past midnight in Dubai
    assert business_days_since(sent, at(DUBAI, 2026, 10, 21, 0, 30), "India") == 0    # still the same day in India

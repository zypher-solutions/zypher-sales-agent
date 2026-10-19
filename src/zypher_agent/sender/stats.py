"""Outreach statistics: is this working, and is it safe?

Numbers are computed from the same records the system acts on (sent messages and the event log), so they cannot
drift from reality. Rates are shown with their denominator, because 1 reply out of 4 emails means nothing.

Meetings booked are NOT counted: the system never sees the calendar. That number stays with you for now.
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from zypher_agent.db import Repository

POSITIVE = {"interested", "question"}
LOW_RATE = 0.01          # below this, after MIN_SENDS first emails, the offer or the targeting is wrong
MIN_SENDS = 300
REALERT_EVERY = 100      # after an alert, stay quiet until this many more people have been emailed


@dataclass
class Period:
    label: str
    first_emails: int = 0
    follow_ups: int = 0
    companies_emailed: int = 0
    replied: int = 0
    positive: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    bounced: int = 0
    opt_outs: int = 0
    complaints: int = 0

    @property
    def reply_rate(self) -> float | None:
        return self.replied / self.companies_emailed if self.companies_emailed else None

    @property
    def bounce_rate(self) -> float | None:
        return self.bounced / self.first_emails if self.first_emails else None


def build_period(repo: Repository, label: str, since: datetime | None) -> Period:
    sent = repo.sent_since(since)
    p = Period(label=label)
    p.first_emails = sum(m.step == 1 for m in sent)
    p.follow_ups = sum(m.step > 1 for m in sent)
    p.companies_emailed = len({m.contact_id for m in sent})
    replies = repo.events_of(["replied"], since)
    first_reply: dict[str, dict] = {}
    for e in replies:  # oldest first: a person's FIRST reply is the one that counts, and only once
        first_reply.setdefault(e["contact_id"], e)
    p.replied = len(first_reply)
    for e in first_reply.values():
        cls = e["payload"].get("class", "other")
        p.by_class[cls] = p.by_class.get(cls, 0) + 1
        p.positive += cls in POSITIVE and not e["payload"].get("opt_out")
        p.opt_outs += bool(e["payload"].get("opt_out"))
        p.complaints += bool(e["payload"].get("complaint"))
    p.bounced = len({e["contact_id"] for e in repo.events_of(["bounced"], since)})
    return p


def build_report(repo: Repository, now: datetime | None = None) -> list[Period]:
    now = now or datetime.now(UTC)
    return [build_period(repo, "Last 7 days", now - timedelta(days=7)), build_period(repo, "All time", None)]


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


def format_report(periods: list[Period]) -> str:
    out = []
    for p in periods:
        out.append(
            f"{p.label}\n"
            f"  Emailed: {p.companies_emailed} companies ({p.first_emails} first emails, {p.follow_ups} follow-ups)\n"
            f"  Replies: {p.replied} ({_pct(p.reply_rate)} of {p.companies_emailed}); "
            f"{p.positive} positive (interested or a question)\n"
            f"  Bounces: {p.bounced} ({_pct(p.bounce_rate)} of first emails) | opted out: {p.opt_outs} | complaints: {p.complaints}"
        )
    allp = periods[-1]
    out.append(f"\n{reading_guide(allp)}")
    return "\n\n".join(out) if len(out) > 1 else out[0]


def reading_guide(p: Period) -> str:
    """One honest sentence on what the all-time numbers mean so far."""
    if p.companies_emailed < 50:
        return f"Too early to judge: only {p.companies_emailed} companies emailed. Typical cold email gets about 3 to 5% replies; look again after 100."
    rate = p.reply_rate or 0
    if p.bounce_rate and p.bounce_rate > 0.02:
        return "Bounce rate is above 2%: pause and check the email addresses before sending more."
    if p.companies_emailed >= MIN_SENDS and rate < LOW_RATE:
        return "Reply rate is under 1% after 300 emails: the offer or the targeting is wrong. More volume will not fix it."
    if rate >= 0.05:
        return "Reply rate is strong (5% or more). Keep the offer, widen the targeting carefully."
    return "Reply rate is in the normal range (typical cold email is about 3 to 5%). Keep testing the offer wording."


def low_reply_rate_warning(companies_emailed: int, replied: int) -> str | None:
    """Pure rule: after 300 emailed companies, fewer than 1% replying means stop and rethink."""
    if companies_emailed < MIN_SENDS:
        return None
    if replied / companies_emailed >= LOW_RATE:
        return None
    return (f"Only {replied} of {companies_emailed} companies have replied ({replied / companies_emailed:.1%}). "
            "Under 1% after 300 emails means the offer or the targeting is wrong, and sending more makes it worse. "
            "Review the offer in icp.yaml and the kind of companies you search for before continuing.")


def check_reply_rate(repo: Repository, alert) -> bool:
    """Alert the owner (once per 100 more emails) when the reply rate is too low. Never pauses sending: it is advice."""
    allp = build_period(repo, "All time", None)
    message = low_reply_rate_warning(allp.companies_emailed, allp.replied)
    if message is None:
        return False
    last = repo.events_of(["low_reply_alert"], None)
    if last and allp.companies_emailed < last[-1]["payload"].get("emailed", 0) + REALERT_EVERY:
        return False
    repo.log_event("low_reply_alert", actor="system", payload={"emailed": allp.companies_emailed, "replied": allp.replied})
    alert("📉 " + message)
    return True

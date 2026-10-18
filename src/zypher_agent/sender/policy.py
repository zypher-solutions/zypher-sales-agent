"""The send policy: the last gate before an email leaves.

A pure function. It is given everything it needs (the message's state, the clock, today's count, the lead's
country) and returns one of three answers, with a reason a human can read:

  allow  send now
  defer  not now, try again at `until` (outside hours, daily cap reached, too soon after the last send)
  block  do not send, a human must act (not approved, paused, opted out, rules broken)

Blocks are checked before defers, so "paused" is never hidden behind "wait until Monday".
These limits are platform guardrails: a tenant may lower the daily cap, never raise it or skip a rule.
No network, no database, no randomness (the spacing gap is passed in), so every rule is unit-tested.
"""
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

UTC = UTC
STOPPED = frozenset({"replied", "unsubscribed", "bounced", "disqualified"})  # lead statuses that end a sequence
HARD_CEILING = 20  # absolute maximum per day while sending from the company's main domain

# Where a lead is, its working days (Monday=0 ... Sunday=6) and its clock. Unknown country: blocked, never guessed.
COUNTRIES: dict[str, tuple[ZoneInfo, frozenset[int]]] = {
    "UAE": (ZoneInfo("Asia/Dubai"), frozenset({0, 1, 2, 3, 4})),      # weekend Sat-Sun
    "Qatar": (ZoneInfo("Asia/Qatar"), frozenset({6, 0, 1, 2, 3})),    # weekend Fri-Sat
    "India": (ZoneInfo("Asia/Kolkata"), frozenset({0, 1, 2, 3, 4})),
    "US": (ZoneInfo("America/New_York"), frozenset({0, 1, 2, 3, 4})),  # Eastern: the safest single choice
}
ALIASES = {
    "uae": "UAE", "united arab emirates": "UAE", "ae": "UAE", "dubai": "UAE", "abu dhabi": "UAE", "sharjah": "UAE",
    "india": "India", "in": "India", "qatar": "Qatar", "qa": "Qatar", "doha": "Qatar",
    "us": "US", "usa": "US", "united states": "US", "united states of america": "US",
}
# Local-time windows when a business owner reads email. Half-open: [start, end).
WINDOWS = ((time(9, 0), time(11, 30)), (time(13, 0), time(16, 0)))

# Warmup ramp: (last day of the step, emails per day). Day 1 is the day of the first send.
RAMP = ((7, 5), (14, 8), (21, 12))
RAMP_AFTER = 15


@dataclass(frozen=True)
class Decision:
    kind: Literal["allow", "defer", "block"]
    reason: str
    until: datetime | None = None


@dataclass(frozen=True)
class SendContext:
    now: datetime                      # timezone-aware
    country: str | None                # as stored on the company; normalised here
    status: str                        # the message's status
    version: int                       # the message's current version
    approved_version: int | None       # the version a human approved, from the approval event
    paused: bool                       # the tenant's kill switch / stop-loss flag
    suppressed: bool                   # address or domain is on the do-not-contact list
    violations: list[str]              # rule names from re-running the guardrails on the stored draft
    step: int                          # 1 = first touch, 2+ = follow-up
    company_already_contacted: bool    # another contact at this company already received step 1
    sent_today: int                    # every message sent today in the sender's timezone, any step
    first_sent_at: datetime | None     # when the very first email left (starts the warmup ramp)
    last_sent_at: datetime | None
    min_gap_seconds: int               # minimum spacing since the last send (the caller adds random jitter)
    env_cap: int | None                # the owner's own, lower, daily cap
    sender_tz: ZoneInfo                # the day boundary for "today" is midnight here
    contact_status: str = "approved"   # the lead's status: a reply, bounce or opt-out ends the sequence


def normalize_country(raw: str | None) -> str | None:
    return ALIASES.get((raw or "").strip().lower())


def daily_cap(first_sent_at: datetime | None, now: datetime, env_cap: int | None, sender_tz: ZoneInfo) -> int:
    """Today's maximum: the warmup ramp, never above the hard ceiling, and never above the owner's own cap."""
    day = 1
    if first_sent_at is not None:
        day = (now.astimezone(sender_tz).date() - first_sent_at.astimezone(sender_tz).date()).days + 1
    cap = RAMP_AFTER
    for last_day, limit in RAMP:
        if day <= last_day:
            cap = limit
            break
    cap = min(cap, HARD_CEILING)
    return cap if env_cap is None else min(cap, max(env_cap, 0))


def _in_window(local: datetime, workdays: frozenset[int]) -> bool:
    return local.weekday() in workdays and any(s <= local.time() < e for s, e in WINDOWS)


def local_window_start(tz: ZoneInfo, workdays: frozenset[int] | set[int], now: datetime) -> datetime:
    """The first window start strictly after `now`, on a working day, as an aware UTC time."""
    local = now.astimezone(tz)
    for offset in range(10):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in workdays:
            continue
        for start, _ in WINDOWS:
            candidate = datetime.combine(day, start, tzinfo=tz)
            if candidate > local:
                return candidate.astimezone(UTC)
    raise RuntimeError("no working day within ten days")  # unreachable: every country has working days


def business_days_since(start: datetime, now: datetime, country: str | None) -> int | None:
    """Working days that have passed between two moments, counted on the LEAD's calendar (None: unknown country)."""
    name = normalize_country(country)
    if name is None:
        return None
    tz, workdays = COUNTRIES[name]
    day, last, count = start.astimezone(tz).date(), now.astimezone(tz).date(), 0
    while day < last:
        day += timedelta(days=1)
        count += day.weekday() in workdays
    return count


def decide(c: SendContext) -> Decision:
    if c.now.tzinfo is None:
        raise ValueError("SendContext.now must be timezone-aware")

    # ---- blocks: a human has to act ----
    if c.status != "approved":
        return Decision("block", f"message is not approved (status: {c.status})")
    if c.approved_version != c.version:
        return Decision("block", f"approval is for version {c.approved_version}, current text is version {c.version}: not approved")
    if c.paused:
        return Decision("block", "sending is paused")
    if c.suppressed:
        return Decision("block", "contact is on the do-not-contact list")
    if c.contact_status in STOPPED:
        return Decision("block", f"the contact has {c.contact_status}: the sequence is over")
    if c.violations:
        return Decision("block", "fails the current guardrails: " + ", ".join(c.violations))
    if c.step == 1 and c.company_already_contacted:
        return Decision("block", "this company already received a first email at another address")
    country = normalize_country(c.country)
    if country is None:
        return Decision("block", f"unknown country {c.country!r}: cannot choose a safe send window")

    # ---- defers: not now ----
    tz, workdays = COUNTRIES[country]
    if not _in_window(c.now.astimezone(tz), workdays):
        return Decision("defer", f"outside {country} business hours", local_window_start(tz, workdays, c.now))

    cap = daily_cap(c.first_sent_at, c.now, c.env_cap, c.sender_tz)
    if c.sent_today >= cap:
        local = c.now.astimezone(c.sender_tz)
        tomorrow = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=c.sender_tz)
        return Decision("defer", f"daily cap reached ({c.sent_today}/{cap})", tomorrow.astimezone(UTC))

    if c.last_sent_at is not None:
        ready = c.last_sent_at + timedelta(seconds=c.min_gap_seconds)
        if c.now < ready:
            return Decision("defer", "too soon after the previous send", ready)

    return Decision("allow", "ok")

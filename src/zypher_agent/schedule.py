"""When `start` drafts new leads by itself: once a day, on chosen weekdays, at or after a local time.

Pure rules plus one database read. The last run is an event in the log (no extra table), so a restart, or a second
copy on another machine, never drafts twice in one day. A missed run is caught up later the same day, never on a
later day: drafts written for yesterday's plan are not wanted on a Saturday.

Lead finding (Apify) costs money and is deliberately not schedulable: it runs only when the owner asks.
"""
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = datetime.weekday()
MAX_DAILY_LIMIT = 50  # well above any sensible day; a typo like 500 should fail, not burn the free model all day
EVENT = "scheduled_draft"


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class DraftSchedule:
    enabled: bool
    limit: int = 0
    at: dtime = dtime(8, 0)
    weekdays: frozenset[int] = frozenset()
    tz: ZoneInfo = ZoneInfo("UTC")


def parse_schedule(enabled: bool, limit: int, at: str, days: str, tz: str) -> DraftSchedule:
    """Settings -> schedule. Bad values stop the program at start-up with a plain message, never a silent default."""
    if not enabled:
        return DraftSchedule(enabled=False)
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at.strip())
    if not m:
        raise ScheduleError(f"DRAFT_TIME must look like 08:00 (24-hour), got {at!r}")
    names = [d.strip().lower() for d in days.split(",") if d.strip()]
    if not names or any(n not in DAYS for n in names):
        raise ScheduleError(f"DRAFT_DAYS must be a comma list of {','.join(DAYS)}, got {days!r}")
    if not 1 <= limit <= MAX_DAILY_LIMIT:
        raise ScheduleError(f"DRAFT_DAILY_LIMIT must be between 1 and {MAX_DAILY_LIMIT}, got {limit}")
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"DRAFT_TIMEZONE is not a known time zone: {tz!r}") from None
    return DraftSchedule(True, limit, dtime(int(m[1]), int(m[2])), frozenset(DAYS.index(n) for n in names), zone)


def draft_due(now: datetime, last_run: date | None, s: DraftSchedule) -> bool:
    """True when today (in the schedule's zone) is a drafting day, the time has come, and today has not run yet."""
    if not s.enabled:
        return False
    local = now.astimezone(s.tz)
    return local.weekday() in s.weekdays and local.time() >= s.at and last_run != local.date()


def last_scheduled_draft(repo, s: DraftSchedule, now: datetime | None = None) -> date | None:
    """The local date of the most recent scheduled run, from the event log (only the last week is read)."""
    since = (now or datetime.now(UTC)) - timedelta(days=8)
    dates = [e["payload"].get("local_date") for e in repo.events_of([EVENT], since)]
    return max((date.fromisoformat(d) for d in dates if d), default=None)


def local_date(now: datetime, s: DraftSchedule) -> str:
    return now.astimezone(s.tz).date().isoformat()

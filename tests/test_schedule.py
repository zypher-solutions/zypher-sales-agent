from datetime import UTC, date, datetime, timedelta

import pytest

from zypher_agent.db import InMemoryRepo
from zypher_agent.schedule import (
    DraftSchedule,
    ScheduleError,
    draft_due,
    last_scheduled_draft,
    parse_schedule,
)

IST = "Asia/Kolkata"
SCHED = parse_schedule(True, 10, "08:00", "mon,tue,wed,thu,fri", IST)


def ist(y, m, d, hh, mm) -> datetime:
    """An IST wall-clock time as an aware UTC moment (IST = UTC+5:30, no daylight saving)."""
    return datetime(y, m, d, hh, mm, tzinfo=UTC) - timedelta(hours=5, minutes=30)


# 2026-10-21 is a Wednesday; 2026-10-24 a Saturday.
def test_due_on_a_weekday_at_the_time():
    assert draft_due(ist(2026, 10, 21, 8, 0), None, SCHED)


def test_not_due_before_the_time():
    assert not draft_due(ist(2026, 10, 21, 7, 59), None, SCHED)


def test_not_due_on_a_weekend():
    assert not draft_due(ist(2026, 10, 24, 9, 0), None, SCHED)


def test_once_a_day():
    assert not draft_due(ist(2026, 10, 21, 15, 0), date(2026, 10, 21), SCHED)
    assert draft_due(ist(2026, 10, 22, 8, 1), date(2026, 10, 21), SCHED)


def test_catches_up_later_the_same_day_only():
    # The server was down at 08:00 and came back at 11:00: draft today.
    assert draft_due(ist(2026, 10, 21, 11, 0), date(2026, 10, 19), SCHED)
    # Back on Saturday after missing Friday: Friday's run is never made up.
    assert not draft_due(ist(2026, 10, 24, 11, 0), date(2026, 10, 22), SCHED)


def test_day_boundary_is_local_time():
    # 01:00 IST on Wednesday is still Tuesday in UTC; the schedule must think in IST.
    early = parse_schedule(True, 5, "00:30", "wed", IST)
    assert draft_due(ist(2026, 10, 21, 1, 0), None, early)


def test_disabled_is_never_due():
    off = parse_schedule(False, 10, "08:00", "mon,tue,wed,thu,fri", IST)
    assert not draft_due(ist(2026, 10, 21, 9, 0), None, off)


@pytest.mark.parametrize("time_, days, limit", [("8am", "mon", 5), ("25:00", "mon", 5), ("08:00", "monday", 5),
                                                ("08:00", "", 5), ("08:00", "mon", 0), ("08:00", "mon", 51)])
def test_bad_settings_fail_loudly(time_, days, limit):
    with pytest.raises(ScheduleError):
        parse_schedule(True, limit, time_, days, IST)


def test_bad_settings_are_ignored_when_disabled():
    assert parse_schedule(False, 0, "nonsense", "", IST) == DraftSchedule(enabled=False)


def test_last_run_comes_from_the_events_log():
    # Stored in the database, so a restart (or the laptop and the server) never drafts twice on one day.
    repo = InMemoryRepo()
    assert last_scheduled_draft(repo, SCHED) is None
    repo.log_event("scheduled_draft", payload={"local_date": "2026-10-20", "limit": 10})
    repo.log_event("scheduled_draft", payload={"local_date": "2026-10-21", "limit": 10})
    assert last_scheduled_draft(repo, SCHED) == date(2026, 10, 21)


class FakePipeline:
    def __init__(self, repo, fail=False):
        self.repo, self.fail, self.runs, self.progress = repo, fail, [], None

    def run(self, limit):
        # the run is already logged when drafting starts, so a crash mid-run cannot repeat it after a restart
        assert any(e["type"] == "scheduled_draft" for e in self.repo.events)
        self.runs.append(limit)
        if self.progress:
            self.progress("researching lead 1")
        if self.fail:
            raise RuntimeError("model down")
        from zypher_agent.pipeline import RunSummary
        return RunSummary()


def _always_due():
    return parse_schedule(True, 10, "00:00", ",".join(("mon", "tue", "wed", "thu", "fri", "sat", "sun")), IST)


def test_start_loop_drafts_once_a_day_and_keeps_the_heartbeat():
    from zypher_agent.__main__ import _maybe_scheduled_draft

    repo, beats = InMemoryRepo(), []
    p = FakePipeline(repo)
    last = _maybe_scheduled_draft(p, repo, _always_due(), "t", lambda: beats.append(1), None)
    last = _maybe_scheduled_draft(p, repo, _always_due(), "t", lambda: beats.append(1), last)
    assert p.runs == [10] and beats == [1] and last is not None
    # a fresh process (restart) reads the database and does not draft again today
    assert _maybe_scheduled_draft(p, repo, _always_due(), "t", lambda: None, None) == last and p.runs == [10]


def test_a_failed_scheduled_run_does_not_stop_the_loop_or_repeat_today():
    from zypher_agent.__main__ import _maybe_scheduled_draft

    repo = InMemoryRepo()
    p = FakePipeline(repo, fail=True)
    last = _maybe_scheduled_draft(p, repo, _always_due(), "t", lambda: None, None)
    _maybe_scheduled_draft(p, repo, _always_due(), "t", lambda: None, last)
    assert p.runs == [10]

from datetime import UTC, datetime, timedelta

from zypher_agent.health import (
    Heartbeat,
    HealthPinger,
    crash_loop_message,
    mask_secrets,
    read_last_error,
    record_start,
    stale_parts,
    write_last_error,
)

T0 = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


def test_fresh_heartbeats_are_healthy(tmp_path):
    hb = Heartbeat(str(tmp_path))
    hb.beat("main", T0), hb.beat("telegram", T0)
    assert stale_parts(str(tmp_path), T0 + timedelta(minutes=5)) == []


def test_a_hung_loop_is_reported_with_its_age(tmp_path):
    hb = Heartbeat(str(tmp_path))
    hb.beat("main", T0), hb.beat("telegram", T0 + timedelta(minutes=42))
    # the main loop may legitimately spend ~20 min drafting 10 leads, so its limit is 45 min; Telegram's is 10 min
    assert stale_parts(str(tmp_path), T0 + timedelta(minutes=44)) == []
    problems = stale_parts(str(tmp_path), T0 + timedelta(minutes=51))
    assert len(problems) == 1 and problems[0].startswith("main loop")


def test_a_missing_heartbeat_is_a_problem(tmp_path):
    Heartbeat(str(tmp_path)).beat("main", T0)
    assert [p.split()[0] for p in stale_parts(str(tmp_path), T0)] == ["telegram"]


def test_no_heartbeat_dir_means_heartbeats_are_off():
    hb = Heartbeat("")
    hb.beat("main")  # does nothing, raises nothing (the laptop default)
    assert not hb.enabled


def test_three_starts_in_half_an_hour_raise_one_alert_an_hour(tmp_path):
    d = str(tmp_path)
    record_start(d, T0)
    record_start(d, T0 + timedelta(minutes=5))
    assert crash_loop_message(d, T0 + timedelta(minutes=5), "server") is None  # two starts: a normal restart
    write_last_error(d, RuntimeError("boom"))
    record_start(d, T0 + timedelta(minutes=10))
    msg = crash_loop_message(d, T0 + timedelta(minutes=10), "server")
    assert "3 times" in msg and "server" in msg and "RuntimeError: boom" in msg
    record_start(d, T0 + timedelta(minutes=12))
    assert crash_loop_message(d, T0 + timedelta(minutes=12), "server") is None  # already told this hour
    for m in (70, 72, 74):
        record_start(d, T0 + timedelta(minutes=m))
    assert crash_loop_message(d, T0 + timedelta(minutes=74), "server") is not None


def test_old_starts_are_forgotten(tmp_path):
    d = str(tmp_path)
    for days in range(5):
        record_start(d, T0 + timedelta(days=days))
    assert len((tmp_path / "starts").read_text().split()) <= 20


def test_errors_never_store_secrets(tmp_path):
    e = RuntimeError("connect to postgresql://postgres.abc:Sup3rS3cret@aws-1.pooler.supabase.com:5432/postgres "
                     "and https://api.telegram.org/bot123456:AAH-secret_TOKEN/getUpdates failed")
    write_last_error(str(tmp_path), e)
    text = read_last_error(str(tmp_path))
    assert "Sup3rS3cret" not in text and "AAH-secret_TOKEN" not in text and "RuntimeError" in text


def test_mask_secrets_keeps_the_useful_part():
    assert mask_secrets("postgresql://u:p@host:5432/db") == "postgresql://***@host:5432/db"
    assert mask_secrets("ping https://hc-ping.com/abc-123 failed") == "ping https://hc-ping.com/*** failed"


def test_healthchecks_ping_every_five_minutes_and_never_raises():
    calls = []

    def get(url, timeout):
        calls.append(url)
        if len(calls) == 2:
            raise OSError("network down")

    p = HealthPinger("https://hc-ping.com/abc", get=get)
    p.maybe_ping(T0)
    p.maybe_ping(T0 + timedelta(minutes=2))   # too soon
    p.maybe_ping(T0 + timedelta(minutes=5))   # fails quietly
    p.maybe_ping(T0 + timedelta(minutes=6))   # a failed ping is retried next cycle, not 5 minutes later
    assert len(calls) == 3
    HealthPinger(None, get=get).maybe_ping(T0)  # no URL configured: nothing happens
    assert len(calls) == 3

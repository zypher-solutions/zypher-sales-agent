"""Is the agent alive? Heartbeat files, a watchdog, a crash-loop alert and an off-server ping.

- Each loop (the main send/inbox loop, the Telegram listener) touches its own file in HEARTBEAT_DIR on every pass.
  A fresh file means "this loop is not stuck", not "everything works": a loop that keeps failing still beats.
- `python -m zypher_agent health` (Docker's HEALTHCHECK) and the in-process watchdog both use `stale_parts`.
  Plain Docker never restarts an "unhealthy" container by itself, so the watchdog exits the process instead and
  `restart: unless-stopped` starts a fresh one.
- A start log spots crash loops; the owner gets one Telegram message an hour about them, at most.
- Healthchecks.io is pinged every 5 minutes. If the pings stop (server down, Telegram down, anything), it emails.

All of it is off when HEARTBEAT_DIR is empty (the laptop default).
"""
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

LIMITS = {
    "main": timedelta(minutes=45),      # drafting 10 leads on the free model can take ~20 minutes in one pass
    "telegram": timedelta(minutes=10),  # the long poll returns at least every 30 s
}
NAMES = {"main": "main loop", "telegram": "telegram listener"}
CRASH_WINDOW, CRASH_STARTS, ALERT_EVERY = timedelta(minutes=30), 3, timedelta(hours=1)
KEEP_STARTS = 20


def _now() -> datetime:
    return datetime.now(UTC)


class Heartbeat:
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory) if directory else None
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    def beat(self, part: str, now: datetime | None = None) -> None:
        if self.dir is not None:
            (self.dir / f"heartbeat-{part}").write_text((now or _now()).isoformat(), encoding="utf-8")


def stale_parts(directory: str, now: datetime | None = None) -> list[str]:
    """Plain-language problems, empty when every loop has beaten recently."""
    now, problems = now or _now(), []
    for part, limit in LIMITS.items():
        f = Path(directory) / f"heartbeat-{part}"
        try:
            age = now - datetime.fromisoformat(f.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            problems.append(f"{NAMES[part]} has no heartbeat yet")
            continue
        if age > limit:
            problems.append(f"{NAMES[part]} silent for {int(age.total_seconds() // 60)} min (limit {limit.seconds // 60})")
    return problems


def start_watchdog(directory: str, on_stall: Callable[[str], None], every: float = 60.0) -> threading.Thread:
    """Check the heartbeats every minute; on a stall, tell the owner, then end the process so Docker restarts it."""
    import time

    def run() -> None:
        while True:
            time.sleep(every)
            problems = stale_parts(directory)
            if problems:
                text = "; ".join(problems)
                try:
                    on_stall(text)
                finally:
                    print(f"WATCHDOG: {text}. Exiting so a fresh copy starts.", flush=True)
                    os._exit(1)  # sys.exit would only end this thread

    t = threading.Thread(target=run, daemon=True, name="watchdog")
    t.start()
    return t


# ---- crash loops ----

def record_start(directory: str, now: datetime | None = None) -> None:
    f = Path(directory) / "starts"
    lines = f.read_text(encoding="utf-8").split() if f.exists() else []
    lines.append((now or _now()).isoformat())
    f.write_text("\n".join(lines[-KEEP_STARTS:]) + "\n", encoding="utf-8")


def crash_loop_message(directory: str, now: datetime | None, host: str) -> str | None:
    """A message when the agent has started too often lately, at most once an hour; otherwise None."""
    now, d = now or _now(), Path(directory)
    starts = [datetime.fromisoformat(s) for s in (d / "starts").read_text(encoding="utf-8").split()]
    recent = [s for s in starts if now - s <= CRASH_WINDOW]
    if len(recent) < CRASH_STARTS:
        return None
    last = d / "last_crash_alert"
    if last.exists() and now - datetime.fromisoformat(last.read_text(encoding="utf-8").strip()) < ALERT_EVERY:
        return None
    last.write_text(now.isoformat(), encoding="utf-8")
    error = read_last_error(directory) or "none recorded"
    return (f"The sales agent on {host} restarted {len(recent)} times in {CRASH_WINDOW.seconds // 60} min. "
            f"Last error: {error}. Logs: docker compose logs --tail 100 sales-agent")


# ---- last error (secrets masked) ----

_SECRET_PATTERNS = [
    (re.compile(r"(\w+://)[^/@\s]+@"), r"\1***@"),                 # user:password@ in any URL
    (re.compile(r"(/bot)\d+:[\w-]+"), r"\1***"),                     # Telegram bot token in an API URL
    (re.compile(r"(hc-ping\.com/)[\w/-]+"), r"\1***"),               # Healthchecks ping URL
]


def mask_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def write_last_error(directory: str, e: BaseException) -> None:
    text = mask_secrets(f"{type(e).__name__}: {e}")[:300]
    (Path(directory) / "last_error").write_text(text, encoding="utf-8")


def read_last_error(directory: str) -> str:
    f = Path(directory) / "last_error"
    return f.read_text(encoding="utf-8") if f.exists() else ""


# ---- off-server monitor ----

class HealthPinger:
    """Pings a Healthchecks.io URL at most every 5 minutes. A failed ping is retried on the next call."""

    def __init__(self, url: str | None, every: timedelta = timedelta(minutes=5), get: Callable | None = None) -> None:
        self.url, self.every, self._last = url, every, None
        if get is None:
            import httpx

            get = httpx.get
        self._get = get

    def maybe_ping(self, now: datetime | None = None) -> None:
        now = now or _now()
        if not self.url or (self._last is not None and now - self._last < self.every):
            return
        try:
            self._get(self.url, timeout=10)
            self._last = now
        except Exception:  # noqa: BLE001 - monitoring must never stop the agent; silence makes Healthchecks alert
            pass

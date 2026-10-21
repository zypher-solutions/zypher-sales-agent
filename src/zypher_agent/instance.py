"""One running copy of `start` per tenant, enforced by the database.

Two copies (say the laptop and the server) would both poll Telegram, so a button tap reaches either at random, and
both would run the sender. A Postgres advisory lock is a named lock that lives as long as the connection holding it:
a second copy cannot take it, and if the first copy dies its connection closes and the lock frees itself. It needs no
table, but it does need a connection that stays the same server session: Supabase's session pooler (port 5432) gives
that; the transaction pooler (6543) would not.

A copy that finds the lock taken WAITS (it does not exit), so a server restart never turns into a crash loop, and a
laptop copy simply takes over if the server goes away.
"""
import time
from collections.abc import Callable

LOCK_SQL = "select pg_try_advisory_lock(hashtext(%s)) as ok"
UNLOCK_SQL = "select pg_advisory_unlock(hashtext(%s)) as ok"


class LockLost(RuntimeError):
    """The lock was lost and another copy holds it now: this copy must stop."""


class InstanceLock:
    """Roles: "poll" (listens to Telegram) and "send" (runs the sender). `start` takes both; the older two-window
    mode (`bot` + `worker`) takes one each, so those two can still run side by side, but never next to `start`."""

    def __init__(self, connect: Callable[[], object], tenant_id: str, label: str,
                 roles: tuple[str, ...] = ("poll", "send")) -> None:
        self._connect, self.label = connect, label
        self.keys = [f"zypher_agent:{role}:{tenant_id}" for role in roles]
        self.key = self.keys[0]
        self._conn = None
        self.held = False

    def try_acquire(self) -> bool:
        try:
            if self._conn is None or self._conn.closed or getattr(self._conn, "broken", False):
                self._conn = self._connect()
            taken = []
            for key in self.keys:
                if not self._conn.execute(LOCK_SQL, (key,)).fetchone()["ok"]:
                    for k in taken:  # all or nothing
                        self._conn.execute(UNLOCK_SQL, (k,))
                    self.held = False
                    return False
                taken.append(key)
            self.held = True
        except Exception:  # noqa: BLE001 - database down: we do not hold it; the caller retries
            self._drop()
        return self.held

    def ensure_held(self) -> None:
        """Called every cycle. Re-takes the lock after a dropped connection; raises LockLost if someone else has it."""
        try:
            self._conn.execute("select 1 as ok", ()).fetchone()
            return
        except Exception:  # noqa: BLE001 - the connection (and with it the lock) is gone
            self._drop()
        if not self.try_acquire():
            if self._conn is None:
                return  # database unreachable: nobody else can take the lock either; try again next cycle
            raise LockLost("another copy took over while this copy's database connection was down")

    def release(self) -> None:
        if self._conn is not None and self.held:
            try:
                for key in self.keys:
                    self._conn.execute(UNLOCK_SQL, (key,))
            except Exception:  # noqa: BLE001 - closing the connection releases it anyway
                pass
        self._drop()

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn, self.held = None, False


def wait_for_lock(
    lock: InstanceLock, notify_once: Callable[[str], None], say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep, beat: Callable[[], None] = lambda: None,
) -> None:
    """Block until this copy holds the lock. Tells the owner once (Telegram), and the log every minute."""
    told = False
    while not lock.try_acquire():
        beat()  # waiting is healthy: the watchdog must not restart a copy that is correctly standing by
        if lock._conn is None:
            say("Cannot reach the database to check for another running copy; retrying in a minute.")
        else:
            msg = (f"Another copy of the agent is already running, so this copy ({lock.label}) is waiting. "
                   "Stop the other copy (laptop: Ctrl+C in its window; server: docker compose stop sales-agent) "
                   "if this one should run instead.")
            say(msg)
            if not told:
                told = True
                notify_once(msg)
        sleep(60)

"""Only one copy of `start` may run per tenant: two would both poll Telegram and both send."""
import os

import pytest

from zypher_agent.instance import InstanceLock, LockLost, wait_for_lock


class FakeDb:
    """Stands in for Postgres advisory locks: one holder per key, released when its connection dies."""

    def __init__(self):
        self.holders: dict[str, "FakeConn"] = {}


class FakeConn:
    def __init__(self, db: FakeDb):
        self.db, self.closed, self.broken = db, False, False

    def execute(self, sql, params=()):
        if self.broken:
            raise OSError("connection lost")
        if "pg_try_advisory_lock" in sql:
            key = params[0]
            holder = self.db.holders.get(key)
            if holder is None or holder.broken or holder.closed:
                self.db.holders[key] = self
            return _Row({"ok": self.db.holders[key] is self})
        if "pg_advisory_unlock" in sql:
            self.db.holders.pop(params[0], None)
        return _Row({"ok": True})

    def close(self):
        self.closed = True


class _Row:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def make(db, tenant="t1", label="server", roles=("poll", "send")):
    return InstanceLock(lambda: FakeConn(db), tenant, label, roles)


def test_start_excludes_bot_and_worker_but_they_can_run_together():
    db = FakeDb()
    bot, worker = make(db, label="bot", roles=("poll",)), make(db, label="worker", roles=("send",))
    assert bot.try_acquire() and worker.try_acquire()  # the older two-window mode still works
    start = make(db, label="start")
    assert not start.try_acquire()
    assert db.holders[bot.key] is bot._conn  # a failed start leaves the others' locks alone
    bot.release()
    assert not start.try_acquire()             # still blocked by the worker
    assert "zypher_agent:poll:t1" not in db.holders  # and it gave back the poll lock it briefly took
    worker.release()
    assert start.try_acquire()


def test_only_one_copy_holds_the_lock():
    db = FakeDb()
    a, b = make(db, label="server"), make(db, label="laptop")
    assert a.try_acquire() and not b.try_acquire()
    a.release()
    assert b.try_acquire()


def test_tenants_do_not_block_each_other():
    db = FakeDb()
    assert make(db, "t1").try_acquire() and make(db, "t2").try_acquire()


def test_a_dropped_connection_is_retaken_when_nobody_else_took_it():
    db = FakeDb()
    a = make(db)
    assert a.try_acquire()
    a._conn.broken = True  # e.g. the pooler restarted
    a.ensure_held()        # reconnects and takes the lock again
    assert db.holders[a.key] is a._conn


def test_a_lost_lock_taken_by_another_copy_raises():
    db = FakeDb()
    a, b = make(db, label="server"), make(db, label="laptop")
    assert a.try_acquire()
    a._conn.broken = True
    assert b.try_acquire()  # the laptop took over while the server's connection was down
    with pytest.raises(LockLost):
        a.ensure_held()


def test_the_waiting_copy_says_so_once_and_keeps_its_heartbeat():
    db = FakeDb()
    holder, waiter = make(db, label="server"), make(db, label="laptop")
    holder.try_acquire()
    said, beats, sleeps = [], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            holder.release()  # the server copy is stopped: the laptop copy takes over

    wait_for_lock(waiter, notify_once=said.append, say=lambda line: None, sleep=sleep, beat=lambda: beats.append(1))
    assert len(said) == 1 and "another copy" in said[0].lower() and "laptop" in said[0]
    assert len(beats) >= 3 and db.holders[waiter.key] is waiter._conn


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_real_postgres_advisory_lock():
    import psycopg
    from psycopg.rows import dict_row

    url = os.environ["TEST_DATABASE_URL"]
    connect = lambda: psycopg.connect(url, autocommit=True, prepare_threshold=None, row_factory=dict_row)
    a, b = InstanceLock(connect, "t-real", "a"), InstanceLock(connect, "t-real", "b")
    try:
        assert a.try_acquire() and not b.try_acquire()
        a.ensure_held()
        a.release()
        assert b.try_acquire()
    finally:
        a.release(), b.release()

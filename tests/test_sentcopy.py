"""Every email we send is also filed in the mailbox's Sent folder, so it shows in the owner's webmail."""
import imaplib
import random
from datetime import timedelta

from pydantic import SecretStr
from test_send_worker import TUE_10AM, World

from zypher_agent.config import Settings
from zypher_agent.sender.sentcopy import IMAPSentSaver, SentCopyError
from zypher_agent.sender.transport import PermanentSendError, TransientSendError


def world_with_saver(saver):
    w = World()
    w.worker.save_copy = saver
    return w


def test_a_successful_send_files_one_copy_with_the_real_message():
    copies = []
    w = world_with_saver(copies.append)
    msg = w.message(w.lead(name="Acme", email="o@acme.ae"))
    assert w.run().sent == 1
    assert len(copies) == 1
    raw = copies[0].decode("utf-8")
    sent = w.transport.sent[0]
    assert f"Message-ID: {sent.message_id}" in raw and "To: o@acme.ae" in raw and "Subject: Quote requests" in raw
    assert "Hi Ann," in raw or "Ann" in raw
    assert w.state(msg.id).status == "sent"


def test_nothing_is_filed_when_the_email_did_not_leave():
    for error in (PermanentSendError("550 no such user"), TransientSendError("451 try later")):
        copies = []
        w = world_with_saver(copies.append)
        w.transport.fail_with = {"o@acme.ae": error}
        w.message(w.lead(name="Acme", email="o@acme.ae"))
        w.run()
        assert copies == [], type(error).__name__


def test_a_failure_to_file_the_copy_never_changes_the_send():
    def broken(raw):
        raise SentCopyError("IMAP4.error: could not save to 'Sent'")

    w = world_with_saver(broken)
    msg = w.message(w.lead(name="Acme", email="o@acme.ae"))
    summary = w.run()
    assert summary.sent == 1 and w.state(msg.id).status == "sent" and len(w.transport.sent) == 1
    assert any(e["type"] == "sent_copy_failed" for e in w.repo.events)
    w.run(TUE_10AM + timedelta(hours=1))
    assert len(w.transport.sent) == 1  # and it is not sent again


def test_no_saver_means_no_copy_and_no_error():
    w = world_with_saver(None)
    w.message(w.lead(name="Acme", email="o@acme.ae"))
    assert w.run().sent == 1 and not any(e["type"] == "sent_copy_failed" for e in w.repo.events)


# ---------- the IMAP side ----------

class FakeIMAP:
    instances = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port, self.calls, self.status = host, port, [], "OK"
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def append(self, folder, flags, date, raw):
        self.calls.append(("append", folder, flags, raw))
        return self.status, [b"done"]

    def logout(self):
        self.calls.append(("logout",))


def saver(monkeypatch, **kw):
    FakeIMAP.instances.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    s = Settings(_env_file=None, sender_email="me@zypher-solutions.com", mail_password=SecretStr("pw"), **kw)
    return IMAPSentSaver(s)


def test_the_copy_is_added_to_the_sent_folder_already_marked_as_read(monkeypatch):
    saver(monkeypatch)(b"RAW MESSAGE")
    calls = FakeIMAP.instances[0].calls
    assert ("append", '"Sent"', "\\Seen", b"RAW MESSAGE") in calls
    assert calls[0] == ("login", "me@zypher-solutions.com", "pw") and calls[-1] == ("logout",)


def test_the_folder_name_is_configurable(monkeypatch):
    saver(monkeypatch, sent_folder="Sent Items")(b"x")
    assert any(c[:2] == ("append", '"Sent Items"') for c in FakeIMAP.instances[0].calls)


def test_a_refusal_or_a_network_error_becomes_a_clear_error_and_always_logs_out(monkeypatch):
    s = saver(monkeypatch)
    original = FakeIMAP.append

    def refuse(self, *a):
        return "NO", [b"[TRYCREATE] no such mailbox"]

    monkeypatch.setattr(FakeIMAP, "append", refuse)
    try:
        s(b"x")
        raised = None
    except SentCopyError as e:
        raised = str(e)
    assert raised and "refused" in raised and ("logout",) in FakeIMAP.instances[0].calls
    monkeypatch.setattr(FakeIMAP, "append", original)

    def explode(host, port, ssl_context=None, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", explode)
    try:
        s(b"x")
        raised = None
    except SentCopyError as e:
        raised = str(e)
    assert raised and "network down" not in raised and "OSError" in raised  # no internals or secrets leak


def test_the_password_is_never_shown(monkeypatch):
    assert "pw" not in repr(saver(monkeypatch))


def test_the_setting_defaults_are_on_and_sent(monkeypatch):
    s = Settings(_env_file=None)
    assert s.save_sent_copy is True and s.sent_folder == "Sent" and random

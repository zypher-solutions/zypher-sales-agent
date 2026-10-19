import smtplib

import pytest
from pydantic import SecretStr

from zypher_agent.config import Settings
from zypher_agent.sender.preflight import FAIL, OK, WARN, check_dns, check_settings
from zypher_agent.sender.transport import (
    AmbiguousSendError,
    FakeTransport,
    OutgoingEmail,
    PermanentSendError,
    SendConfigError,
    SMTPTransport,
    TransientSendError,
    build_mime,
    new_message_id,
)

PASSWORD = "mailbox-secret-pw"


def email(**kw) -> OutgoingEmail:
    base = {"to": "owner@acme.ae", "subject": "quote requests", "body": "Hi there,\n\nIs it by hand?\n\nBest,\nMuhammed",
            "message_id": "<abc@zypher-solutions.com>", "from_addr": "me@zypher-solutions.com", "from_name": "Muhammed Hasheem"}
    return OutgoingEmail(**{**base, **kw})


# ---------- the message we build ----------

def test_message_is_plain_text_with_clean_headers():
    m = build_mime(email())
    assert m["From"] == "Muhammed Hasheem <me@zypher-solutions.com>" and m["To"] == "owner@acme.ae"
    assert m["Message-ID"] == "<abc@zypher-solutions.com>" and m["Reply-To"] == "me@zypher-solutions.com"
    assert m["List-Unsubscribe"] == "<mailto:me@zypher-solutions.com?subject=unsubscribe>"
    assert m.get_content_type() == "text/plain" and not m.is_multipart()  # no HTML, no tracking pixel
    assert "Is it by hand?" in m.get_content()
    assert "In-Reply-To" not in m and "References" not in m


def test_follow_ups_are_threaded_to_the_previous_email():
    m = build_mime(email(in_reply_to="<first@zypher-solutions.com>", subject="Re: quote requests"))
    assert m["In-Reply-To"] == "<first@zypher-solutions.com>" and m["References"] == "<first@zypher-solutions.com>"


def test_message_ids_are_unique_and_on_the_senders_domain():
    a, b = new_message_id("me@zypher-solutions.com"), new_message_id("me@zypher-solutions.com")
    assert a != b and a.endswith("@zypher-solutions.com>")


# ---------- how SMTP failures are sorted ----------

class FakeServer:
    def __init__(self, error=None):
        self.error, self.sent, self.closed = error, [], False

    def send_message(self, msg):
        if self.error:
            raise self.error
        self.sent.append(msg)

    def quit(self):
        self.closed = True


def transport(server=None, connect_error=None) -> SMTPTransport:
    t = SMTPTransport(Settings(_env_file=None, sender_email="me@zypher-solutions.com", mail_password=SecretStr(PASSWORD)))

    def connect():
        if connect_error:
            raise connect_error
        return server
    t._connect = connect
    return t


def test_successful_send_closes_the_connection():
    server = FakeServer()
    transport(server).send(email())
    assert len(server.sent) == 1 and server.closed


@pytest.mark.parametrize(("error", "expected"), [
    (smtplib.SMTPRecipientsRefused({"owner@acme.ae": (550, b"no such user")}), PermanentSendError),
    (smtplib.SMTPDataError(554, b"message rejected as spam"), PermanentSendError),
    (smtplib.SMTPResponseException(451, b"try again later"), TransientSendError),
    (smtplib.SMTPServerDisconnected("gone"), AmbiguousSendError),
    (TimeoutError(), AmbiguousSendError),
])
def test_errors_while_sending_are_sorted(error, expected):
    server = FakeServer(error)
    with pytest.raises(expected):
        transport(server).send(email())
    assert server.closed


def test_connection_failures_before_the_message_are_safe_to_retry_or_fatal():
    with pytest.raises(SendConfigError):
        transport(connect_error=smtplib.SMTPAuthenticationError(535, b"bad credentials")).send(email())
    with pytest.raises(TransientSendError):
        transport(connect_error=OSError("network down")).send(email())


def test_password_is_required_and_never_shown():
    with pytest.raises(SendConfigError):
        SMTPTransport(Settings(_env_file=None, sender_email="me@x.com"))
    t = transport(FakeServer())
    assert PASSWORD not in repr(t) and PASSWORD not in str(t.__dict__.get("user"))


def test_fake_transport_records_and_can_fail_on_demand():
    t = FakeTransport(fail_with={"bad@x.com": PermanentSendError("550")})
    t.send(email())
    with pytest.raises(PermanentSendError):
        t.send(email(to="bad@x.com"))
    assert [e.to for e in t.sent] == ["owner@acme.ae"]


# ---------- preflight ----------

def dns_world(spf="v=spf1 include:secureserver.net -all", dmarc="v=DMARC1; p=quarantine", mx=("mailstore1.secureserver.net",), dkim=None):
    records = {"zypher-solutions.com": [spf] if spf else [], "_dmarc.zypher-solutions.com": [dmarc] if dmarc else []}
    if dkim:
        records[f"{dkim}._domainkey.zypher-solutions.com"] = ["v=DKIM1; k=rsa; p=abc"]
    return (lambda name: records.get(name, [])), (lambda name: list(mx))


def run(**kw):
    txt, mx = dns_world(**kw)
    return {c.name: c for c in check_dns("zypher-solutions.com", "smtpout.secureserver.net", txt, mx)}


def test_preflight_passes_a_correct_setup():
    got = run(dkim="selector1")
    assert [got[n].status for n in ("MX records", "SPF", "DMARC", "DKIM")] == [OK, OK, OK, OK]


def test_preflight_fails_spf_that_does_not_cover_the_server_we_send_through():
    got = run(spf="v=spf1 include:_spf.google.com -all")
    assert got["SPF"].status == FAIL and "secureserver.net" in got["SPF"].detail
    assert run(spf=None)["SPF"].status == FAIL


def test_preflight_warns_on_weak_dmarc_and_unverifiable_dkim():
    assert run(dmarc="v=DMARC1; p=none")["DMARC"].status == WARN
    assert run(dmarc=None)["DMARC"].status == WARN
    assert run()["DKIM"].status == WARN and "dkim=pass" in run()["DKIM"].detail


def test_preflight_fails_a_domain_that_cannot_receive_replies():
    assert run(mx=())["MX records"].status == FAIL


def test_preflight_settings_checks():
    s = Settings(_env_file=None, sender_timezone="Nowhere/Land", daily_send_cap=3)
    got = {c.name: c for c in check_settings(s, ["opt_out_line is empty"])}
    assert got["Email footer"].status == FAIL and got["Timezone"].status == FAIL and "3 a day" in got["Daily cap"].detail


def test_preflight_checks_the_logo_only_when_one_is_set(tmp_path):
    def logo_check(path):
        s = Settings(_env_file=None, sender_logo_path=path)
        return {c.name: c for c in check_settings(s, [])}.get("Logo")

    assert logo_check("") is None
    assert logo_check(str(tmp_path / "gone.png")).status == WARN
    (tmp_path / "logo.jpg").write_bytes(b"\xff\xd8 jpeg")
    assert "not a PNG" in logo_check(str(tmp_path / "logo.jpg")).detail
    (tmp_path / "big.png").write_bytes(b"\x89PNG" + b"0" * 60_000)
    assert "under 50 KB" in logo_check(str(tmp_path / "big.png")).detail
    (tmp_path / "ok.png").write_bytes(b"\x89PNG" + b"0" * 6_000)
    assert logo_check(str(tmp_path / "ok.png")).status == OK


def test_a_styled_copy_makes_a_two_part_message_with_the_plain_text_first():
    m = build_mime(email(html="<p>Hi</p>"))
    assert m.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in m.iter_parts()]
    assert kinds == ["text/plain", "text/html"] and not any(k.startswith("image/") for k in kinds)


def test_the_logo_is_embedded_next_to_the_styled_copy():
    png = b"\x89PNG\r\n\x1a\nfake"
    m = build_mime(email(html='<p>Hi</p><img src="cid:logo.1@zypher-solutions.com">',
                         inline_images=(("logo.1@zypher-solutions.com", png),)))
    plain, related = list(m.iter_parts())
    assert plain.get_content_type() == "text/plain"  # plain text still first and unchanged
    assert related.get_content_type() == "multipart/related"
    html_part, image = list(related.iter_parts())
    assert html_part.get_content_type() == "text/html" and image.get_content_type() == "image/png"
    assert image["Content-ID"] == "<logo.1@zypher-solutions.com>" and image.get_content() == png
    assert image.get_content_disposition() == "inline"  # shown in the signature, not listed as an attachment


def test_pictures_are_never_added_to_a_plain_only_email():
    m = build_mime(email(inline_images=(("logo.1@x", b"png"),)))
    assert m.get_content_type() == "text/plain" and not m.is_multipart()

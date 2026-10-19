"""Contract tests: the same behaviour is required of every Repository.

The Postgres run needs a throwaway database: set TEST_DATABASE_URL to enable it.
"""
import os
import uuid

import pytest

from zypher_agent.db import InMemoryRepo, PostgresRepo
from zypher_agent.models import (
    CompanyIn,
    ContactIn,
    Hypothesis,
    NewMessage,
    ResearchReport,
)


@pytest.fixture(params=["memory", "postgres"])
def repo(request):
    if request.param == "memory":
        yield InMemoryRepo()
        return
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    r = PostgresRepo(url)
    yield r
    r.conn.execute("delete from companies where source = 'test'")  # cascades to contacts, messages
    r.conn.execute("delete from suppression_list where reason = 'test'")


TENANT_B = "00000000-0000-0000-0000-0000000000b2"


@pytest.fixture(params=["memory", "postgres"])
def two_tenants(request):
    """(repo for the default tenant, repo for a second tenant) over the same database."""
    if request.param == "memory":
        a = InMemoryRepo()
        yield a, a.for_tenant(TENANT_B)
        return
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    a = PostgresRepo(url)
    a.conn.execute(
        "insert into tenants (id, slug, name) values (%s, 'test-b', 'Test B') on conflict do nothing", (TENANT_B,)
    )
    b = PostgresRepo(url, TENANT_B)
    yield a, b
    a.conn.execute("delete from companies where source = 'test'")
    a.conn.execute("delete from suppression_list where reason = 'test'")
    a.conn.execute("delete from events where tenant_id = %s", (TENANT_B,))
    a.conn.execute("delete from tenants where id = %s", (TENANT_B,))


def new_lead(repo, email="a@acme-test.com", domain=None):
    domain = domain or f"{uuid.uuid4().hex[:8]}.test.com"
    return repo.upsert_lead(
        CompanyIn(name="Acme", domain=domain, source="test"),
        ContactIn(full_name="Ann", email=email),
    ), domain


def test_upsert_is_idempotent_and_enriches(repo):
    first, domain = new_lead(repo)
    again = repo.upsert_lead(
        CompanyIn(name="Acme", domain=domain, industry="Logistics", source="test"),
        ContactIn(email="a@acme-test.com"),
    )
    assert first == again
    lead = repo.get_lead(first)
    assert lead.industry == "Logistics" and lead.contact_name == "Ann"  # None did not overwrite


def test_status_queue(repo):
    cid, _ = new_lead(repo)
    assert cid in [l.contact_id for l in repo.leads_with_status("new", 1000)]
    repo.set_lead_status(cid, "researched")
    assert cid not in [l.contact_id for l in repo.leads_with_status("new", 1000)]
    assert cid in [l.contact_id for l in repo.leads_with_status("researched", 1000)]


def test_suppression_by_email_and_domain_case_insensitive(repo):
    mail, dom = f"{uuid.uuid4().hex[:6]}@Blocked-Test.com", f"{uuid.uuid4().hex[:6]}-blocked.com"
    repo.add_suppression("test", email=mail)
    repo.add_suppression("test", domain=dom)
    assert repo.is_suppressed(mail.upper(), None)
    assert repo.is_suppressed(None, dom.upper())
    assert not repo.is_suppressed("fine@ok-test.com", "ok-test.com")


def test_research_roundtrip(repo):
    cid, _ = new_lead(repo)
    company_id = repo.get_lead(cid).company_id
    assert repo.latest_research(company_id) is None
    h = Hypothesis(id="h1", hypothesis="x", quote="q", source_url="https://a.com", confidence="low")
    repo.save_research(company_id, ResearchReport(services=["s"], hypotheses=[h], model="m"))
    got = repo.latest_research(company_id)
    assert got.services == ["s"] and got.hypotheses[0].id == "h1" and got.model == "m"


def test_message_lifecycle(repo):
    cid, _ = new_lead(repo)
    m = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b", evidence={"k": [1]}))
    assert m.status == "draft" and m.evidence == {"k": [1]}
    m = repo.update_message(m.id, status="needs_changes", telegram_chat_id=42, telegram_message_id=7)
    assert repo.message_needing_changes(42).id == m.id
    assert repo.message_needing_changes(43) is None
    m = repo.update_message(m.id, status="awaiting_approval", version=2, body="b2")
    assert repo.get_message(m.id).version == 2
    assert repo.message_needing_changes(42) is None


def test_latest_message_and_industry_tag(repo):
    cid, _ = new_lead(repo)
    assert repo.latest_message(cid) is None
    repo.save_message(NewMessage(contact_id=cid, subject="s1", body="b"))
    second = repo.save_message(NewMessage(contact_id=cid, subject="s2", body="b"))
    assert repo.latest_message(cid).id == second.id
    company_id = repo.get_lead(cid).company_id
    repo.set_company_industry(company_id, "Logistics")
    repo.set_company_industry(company_id, "Retail")  # does not overwrite
    assert repo.get_lead(cid).industry == "Logistics"


def test_update_message_rejects_unknown_fields(repo):
    cid, _ = new_lead(repo)
    m = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
    with pytest.raises(ValueError):
        repo.update_message(m.id, status="sent", id="x; drop table messages")


def test_log_event(repo):
    cid, _ = new_lead(repo)
    repo.log_event("llm_call", contact_id=cid, payload={"cost_usd": 0.01})


# --- tenant isolation: one tenant must never see or touch another's data ---

def test_same_domain_in_two_tenants_is_two_leads(two_tenants):
    a, b = two_tenants
    domain = f"{uuid.uuid4().hex[:8]}.test.com"
    ca = a.upsert_lead(CompanyIn(name="Acme", domain=domain, source="test"), ContactIn(email="x@acme-test.com"))
    cb = b.upsert_lead(CompanyIn(name="Acme", domain=domain, source="test"), ContactIn(email="x@acme-test.com"))
    assert ca != cb
    assert a.get_lead(ca).company_id != b.get_lead(cb).company_id


def test_leads_are_invisible_across_tenants(two_tenants):
    a, b = two_tenants
    cid, _ = new_lead(a)
    assert cid in [l.contact_id for l in a.leads_with_status("new", 1000)]
    assert cid not in [l.contact_id for l in b.leads_with_status("new", 1000)]
    with pytest.raises(KeyError):
        b.get_lead(cid)
    b.set_lead_status(cid, "approved")  # no-op for another tenant's lead
    assert cid in [l.contact_id for l in a.leads_with_status("new", 1000)]
    b.set_company_industry(a.get_lead(cid).company_id, "Hacked")
    assert a.get_lead(cid).industry is None


def test_suppression_is_per_tenant(two_tenants):
    a, b = two_tenants
    mail, dom = f"{uuid.uuid4().hex[:6]}@iso-test.com", f"{uuid.uuid4().hex[:6]}-iso.com"
    a.add_suppression("test", email=mail)
    a.add_suppression("test", domain=dom)
    assert a.is_suppressed(mail, None) and a.is_suppressed(None, dom)
    assert not b.is_suppressed(mail, None) and not b.is_suppressed(None, dom)
    b.add_suppression("test", email=mail)  # the same address may be suppressed independently
    assert b.is_suppressed(mail, None)


def test_messages_are_invisible_across_tenants(two_tenants):
    a, b = two_tenants
    cid, _ = new_lead(a)
    m = a.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
    a.update_message(m.id, status="needs_changes", telegram_chat_id=42)
    with pytest.raises(KeyError):
        b.get_message(m.id)
    with pytest.raises(KeyError):
        b.update_message(m.id, status="approved")
    assert b.latest_message(cid) is None
    assert b.message_needing_changes(42) is None
    assert a.get_message(m.id).status == "needs_changes"


def test_cannot_attach_data_to_another_tenants_records(two_tenants):
    a, b = two_tenants
    cid, _ = new_lead(a)
    company_id = a.get_lead(cid).company_id
    with pytest.raises(KeyError):
        b.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
    with pytest.raises(KeyError):
        b.save_research(company_id, ResearchReport(services=["s"], model="m"))
    a.save_research(company_id, ResearchReport(services=["s"], model="m"))
    assert b.latest_research(company_id) is None
    assert a.latest_research(company_id) is not None


def test_postgres_repo_reconnects_after_a_broken_connection():
    class FakeConn:
        def __init__(self, broken=False):
            self.closed, self.broken, self.ran = False, broken, []

        def execute(self, sql, params=None):
            self.ran.append(sql)

    repo = PostgresRepo.__new__(PostgresRepo)  # skip the real connect
    repo.tenant_id, repo.conn = "t", FakeConn(broken=True)
    fresh = FakeConn()
    repo._connect = lambda: fresh
    repo._exec("select 1")
    assert repo.conn is fresh and fresh.ran == ["select 1"]


def test_company_exists_and_contact_email_are_tenant_scoped(two_tenants):
    a, b = two_tenants
    domain = f"{uuid.uuid4().hex[:8]}.test.com"
    cid = a.upsert_lead(CompanyIn(name="Src", domain=domain, source="test"), ContactIn(phone="+971 1"))
    assert a.company_exists(domain) and not b.company_exists(domain)
    lead = a.get_lead(cid)
    assert lead.email is None and lead.phone == "+971 1"
    b.set_contact_email(cid, "hijack@evil.com")  # other tenant: no effect
    a.set_contact_email(cid, f"info@{domain}")
    assert a.get_lead(cid).email == f"info@{domain}"


# --- sending queries: same behaviour in memory and in Postgres ---

def approved_message(repo, cid, step=1, version=1):
    m = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b", step=step, version=version))
    repo.update_message(m.id, status="approved")
    repo.log_event("approved", contact_id=cid, message_id=m.id, actor="human:1", payload={"version": version})
    return m


def test_sending_queries(repo):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    cid, _ = new_lead(repo)
    m = approved_message(repo, cid)
    assert [x.id for x in repo.approved_to_send(now)] == [m.id] or m.id in [x.id for x in repo.approved_to_send(now)]
    assert repo.approved_version(m.id) == 1
    repo.update_message(m.id, send_after=now + timedelta(hours=1))
    assert m.id not in [x.id for x in repo.approved_to_send(now)]  # deferred: not due yet
    repo.update_message(m.id, send_after=None)

    assert repo.claim_for_sending(m.id, "<one@x.test>", "me@x.test") is True
    assert repo.claim_for_sending(m.id, "<two@x.test>", "me@x.test") is False  # already claimed
    got = repo.get_message(m.id)
    assert got.status == "queued" and got.send_attempts == 1 and got.message_id_header == "<one@x.test>"
    assert m.id not in [x.id for x in repo.approved_to_send(now)]

    day = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
    assert repo.send_stats(day).sent_today == 0 and repo.first_message_header(cid) is None
    repo.update_message(m.id, status="sent", sent_at=now)
    stats = repo.send_stats(day)
    assert (stats.sent_today, stats.first_sent_at, stats.last_sent_at) == (1, now, now)
    assert repo.send_stats(day + timedelta(days=1)).sent_today == 0  # yesterday's sends do not count today
    assert repo.first_message_header(cid) == ("<one@x.test>", "s")


def test_company_contacted_ignores_the_contact_itself(repo):
    from datetime import datetime, timezone
    domain = f"{uuid.uuid4().hex[:8]}.test.com"
    a = repo.upsert_lead(CompanyIn(name="Co", domain=domain, source="test"), ContactIn(email=f"a@{domain}"))
    b = repo.upsert_lead(CompanyIn(name="Co", domain=domain, source="test"), ContactIn(email=f"b@{domain}"))
    company = repo.get_lead(a).company_id
    m = repo.save_message(NewMessage(contact_id=a, subject="s", body="b"))
    repo.update_message(m.id, status="sent", sent_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert repo.company_contacted(company, b) is True
    assert repo.company_contacted(company, a) is False


def test_pause_flag_is_per_tenant(two_tenants):
    a, b = two_tenants
    assert a.sending_state() == (False, None)
    a.set_sending_paused(True, "bounce spike")
    try:
        assert a.sending_state() == (True, "bounce spike") and b.sending_state()[0] is False
    finally:
        a.set_sending_paused(False)
    assert a.sending_state() == (False, None)


# --- inbox queries ---

def test_inbox_queries(repo):
    from datetime import datetime, timezone
    cid, domain = new_lead(repo, email=f"Owner@{uuid.uuid4().hex[:6]}.test.com")
    lead = repo.get_lead(cid)
    out = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
    repo.update_message(out.id, status="sent", sent_at=datetime(2026, 10, 20, tzinfo=timezone.utc), message_id_header=f"<{uuid.uuid4().hex}@x.test>")
    sent = repo.get_message(out.id)
    assert repo.message_by_header(sent.message_id_header).id == out.id and repo.message_by_header("<nope@x>") is None
    assert repo.lead_by_email(lead.email.upper()).contact_id == cid  # case-insensitive
    assert repo.lead_by_email("nobody@nowhere.test") is None
    assert out.id in [m.id for m in repo.recent_sent(100)]

    # a reply we received must never be mistaken for our own latest email
    inbound = repo.save_message(NewMessage(contact_id=cid, subject="Re: s", body="yes", direction="inbound", status="delivered", reply_class="interested"))
    got = repo.get_message(inbound.id)
    assert got.direction == "inbound" and got.reply_class == "interested"
    assert repo.latest_message(cid).id == out.id
    assert inbound.id not in [m.id for m in repo.recent_sent(100)]


def test_inbox_cursor_roundtrip(repo):
    before = repo.inbox_cursor()
    repo.set_inbox_cursor(7, 100)
    repo.set_inbox_cursor(7, 105)
    assert repo.inbox_cursor() == (7, 105)
    if before is not None:  # live-shaped database: leave the cursor as we found it
        repo.set_inbox_cursor(*before)


def test_inbox_matching_is_tenant_scoped(two_tenants):
    from datetime import datetime, timezone
    a, b = two_tenants
    cid, _ = new_lead(a, email=f"o@{uuid.uuid4().hex[:6]}.test.com")
    header = f"<{uuid.uuid4().hex}@x.test>"
    m = a.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
    a.update_message(m.id, status="sent", sent_at=datetime(2026, 10, 20, tzinfo=timezone.utc), message_id_header=header)
    assert b.message_by_header(header) is None and b.lead_by_email(a.get_lead(cid).email) is None and b.recent_sent(100) == []


# --- follow-up queries ---

def test_follow_up_queries(repo):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    cid, _ = new_lead(repo)
    first = repo.save_message(NewMessage(contact_id=cid, subject="s1", body="b1"))
    repo.update_message(first.id, status="sent", sent_at=t0, message_id_header=f"<{uuid.uuid4().hex}@x.test>")
    assert cid not in [lead.contact_id for lead, _, _ in repo.sequence_candidates()]  # status is not in_sequence yet
    repo.set_lead_status(cid, "in_sequence")
    mine = [(lead, step, at) for lead, step, at in repo.sequence_candidates() if lead.contact_id == cid]
    assert len(mine) == 1 and mine[0][1] == 1 and mine[0][2] == t0 and mine[0][0].status == "in_sequence"

    second = repo.save_message(NewMessage(contact_id=cid, subject="s2", body="b2", step=2))
    repo.update_message(second.id, status="sent", sent_at=t0 + timedelta(days=5))
    assert [(s, m.step) for lead, s, _ in repo.sequence_candidates() if lead.contact_id == cid for m in [second]] == [(2, 2)]
    assert [m.step for m in repo.sent_messages(cid)] == [1, 2]

    # unsent mail to this contact is withdrawn; sent mail is history
    waiting = repo.save_message(NewMessage(contact_id=cid, subject="s3", body="b3", step=3, status="awaiting_approval"))
    approved = repo.save_message(NewMessage(contact_id=cid, subject="s3", body="b3", step=3))
    repo.update_message(approved.id, status="approved")
    assert repo.cancel_unsent(cid, "contact replied") == 2
    assert repo.get_message(waiting.id).status == "rejected" and repo.get_message(approved.id).status == "rejected"
    assert repo.get_message(second.id).status == "sent" and repo.cancel_unsent(cid, "again") == 0


def test_has_event_since(repo):
    from datetime import datetime, timedelta, timezone
    cid, _ = new_lead(repo)
    now = datetime.now(timezone.utc)
    assert repo.has_event_since(cid, "out_of_office", now - timedelta(days=7)) is False
    repo.log_event("out_of_office", contact_id=cid, payload={})
    assert repo.has_event_since(cid, "out_of_office", now - timedelta(days=7)) is True
    assert repo.has_event_since(cid, "out_of_office", now + timedelta(days=1)) is False
    assert repo.has_event_since(cid, "replied", now - timedelta(days=7)) is False


# --- statistics queries ---

def test_statistics_queries(repo):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cid, _ = new_lead(repo)
    m = repo.save_message(NewMessage(contact_id=cid, subject="s", body="b"))
    repo.update_message(m.id, status="sent", sent_at=now - timedelta(days=2))
    assert m.id in [x.id for x in repo.sent_since(None)]
    assert m.id in [x.id for x in repo.sent_since(now - timedelta(days=7))]
    assert m.id not in [x.id for x in repo.sent_since(now - timedelta(days=1))]
    repo.log_event("replied", contact_id=cid, payload={"class": "interested", "opt_out": False})
    repo.log_event("bounced", contact_id=cid, payload={})
    got = [e for e in repo.events_of(["replied"], now - timedelta(minutes=5)) if e["contact_id"] == cid]
    assert len(got) == 1 and got[0]["payload"]["class"] == "interested" and got[0]["type"] == "replied"
    assert not [e for e in repo.events_of(["replied"], now + timedelta(minutes=5)) if e["contact_id"] == cid]


def test_research_keeps_the_proof_of_a_proper_company(repo):
    cid, _ = new_lead(repo)
    company_id = repo.get_lead(cid).company_id
    proof = [{"kind": "office_team", "quote": "Our sales and accounts teams work from our head office", "source_url": "https://a.com/about"}]
    repo.save_research(company_id, ResearchReport(services=["s"], qualification=proof, model="m"))
    assert repo.latest_research(company_id).qualification == proof

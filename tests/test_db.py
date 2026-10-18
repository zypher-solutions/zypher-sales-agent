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

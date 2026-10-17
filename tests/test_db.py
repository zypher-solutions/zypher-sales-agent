"""Contract tests: the same behaviour is required of every Repository.

The Postgres run needs a throwaway database: set TEST_DATABASE_URL to enable it.
"""
import os
import uuid

import pytest

from zypher_agent.db import InMemoryRepo, PostgresRepo
from zypher_agent.models import CompanyIn, ContactIn, Hypothesis, NewMessage, ResearchReport


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

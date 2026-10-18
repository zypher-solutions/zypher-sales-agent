import json

import httpx
import pytest

from zypher_agent.db import InMemoryRepo
from zypher_agent.dry_run import build
from zypher_agent.leads import rank_contact_emails
from zypher_agent.models import CompanyIn, ContactIn
from zypher_agent.sources.apify_maps import (
    ApifyMapsSource,
    SourceError,
    estimate_cost_usd,
    parse_items,
)
from zypher_agent.sources.base import SourcedCompany, import_sourced

TOKEN = "apify_api_test_secret"


def place(title, website="https://www.acme-cargo.ae/", **kw):
    return {"title": title, "website": website, "countryCode": "AE", "categoryName": "Logistics service",
            "phone": "+971 4 000 0000", "address": "Al Quoz, Dubai", "placeId": f"pid-{title}",
            "totalScore": 4.5, "reviewsCount": 80, "permanentlyClosed": False, "temporarilyClosed": False, **kw}


# ---------- parsing actor output ----------

def test_parse_keeps_companies_with_their_own_site():
    found, skipped = parse_items([place("Acme Cargo")])
    assert skipped == []
    c = found[0]
    assert (c.name, c.domain, c.country, c.industry, c.phone) == (
        "Acme Cargo", "acme-cargo.ae", "UAE", "Logistics service", "+971 4 000 0000")
    assert c.source == "apify_maps" and c.source_ref == "pid-Acme Cargo"


@pytest.mark.parametrize(("item", "reason"), [
    (place("No Site", website=""), "no website"),
    (place("Closed", permanentlyClosed=True), "closed"),
    (place("Paused", temporarilyClosed=True), "closed"),
    (place("FB Only", website="https://facebook.com/fbonly"), "facebook.com"),
    (place("Wix Free", website="https://joe.wixsite.com/cargo"), "wixsite.com"),
    (place("Netlify", website="https://fast-track-cargo.netlify.app/"), "netlify.app"),
    (place("App builder", website="https://citytalk.base44.app/"), "base44.app"),
])
def test_parse_skips_places_we_cannot_research(item, reason):
    found, skipped = parse_items([item])
    assert found == [] and reason in skipped[0][1]


def test_parse_drops_duplicate_domains_and_unnamed_places():
    found, skipped = parse_items([place("A"), place("A branch 2"), {"website": "https://x.com"}])
    assert [c.name for c in found] == ["A"] and skipped == [("A branch 2", "duplicate domain in results")]


# ---------- the Apify call ----------

def source_with(response: httpx.Response):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return ApifyMapsSource(TOKEN, httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_search_sends_filters_auth_header_and_a_hard_cost_cap():
    src, seen = source_with(httpx.Response(201, json=[place("Acme Cargo")]))
    found = src.search("logistics company", "Dubai, United Arab Emirates", 30)
    assert [c.domain for c in found] == ["acme-cargo.ae"]
    req = seen[0]
    body = json.loads(req.content)
    assert body["searchStringsArray"] == ["logistics company"] and body["maxCrawledPlacesPerSearch"] == 30
    assert body["website"] == "withWebsite" and body["skipClosedPlaces"] is True
    assert "scrapeContacts" not in body  # we don't buy contact data; research reads their own site
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(req.url)  # never in the URL, where it could end up in logs
    assert float(req.url.params["maxTotalChargeUsd"]) >= 0.5


def test_errors_are_reported_without_the_token():
    src, _ = source_with(httpx.Response(402, json={"error": {"message": "Not enough credit"}}))
    with pytest.raises(SourceError, match="402.*Not enough credit") as e:
        src.search("q", "Dubai", 5)
    assert TOKEN not in str(e.value) and TOKEN not in repr(src)


def test_result_count_is_bounded_and_cost_is_estimated():
    src, _ = source_with(httpx.Response(201, json=[]))
    with pytest.raises(SourceError):
        src.search("q", "Dubai", 0)
    with pytest.raises(SourceError):
        src.search("q", "Dubai", 101)
    assert estimate_cost_usd(20) == pytest.approx(0.087)


# ---------- importing ----------

def sourced(name, domain):
    return SourcedCompany(name=name, domain=domain, website=f"https://{domain}", phone="+971 1", source="apify_maps")


def test_import_adds_new_companies_without_email_and_is_idempotent():
    repo = InMemoryRepo()
    first = import_sourced(repo, [sourced("Acme", "acme.ae"), sourced("Beta", "beta.ae")])
    again = import_sourced(repo, [sourced("Acme", "acme.ae")])
    assert first.imported == ["Acme", "Beta"] and again.already_known == ["Acme"] and again.imported == []
    lead = repo.leads_with_status("new", 10)[0]
    assert lead.email is None and lead.phone == "+971 1"


def test_import_skips_suppressed_and_existing_csv_domains():
    repo = InMemoryRepo()
    repo.add_suppression("manual", domain="blocked.ae")
    repo.upsert_lead(CompanyIn(name="Old", domain="old.ae"), ContactIn(email="a@old.ae"))
    r = import_sourced(repo, [sourced("Blocked", "blocked.ae"), sourced("Old", "old.ae")])
    assert r.suppressed == ["Blocked"] and r.already_known == ["Old"] and r.imported == []


# ---------- finding the contact email during research ----------

def test_rank_prefers_own_domain_and_drops_wrong_inboxes():
    found = ["noreply@acme.ae", "careers@acme.ae", "owner@gmail.com", "info@acme.ae", "sales@mail.acme.ae"]
    assert rank_contact_emails(found, "acme.ae") == ["info@acme.ae", "sales@mail.acme.ae", "owner@gmail.com"]


def sourced_lead_run(site_emails, email_ok=lambda e: True):
    """A dry-run world plus one sourced lead (no email) whose site lists `site_emails`."""
    run = build(echo=False)
    for c in list(run.repo.contacts.values()):
        c["status"] = "done"  # park the canned leads; only the sourced one runs
    site = run.pipeline.fetcher.sites["acme-freight-demo.com"]
    for p in site.pages:
        p.emails = list(site_emails)
    run.repo.companies.clear()
    import_sourced(run.repo, [SourcedCompany(
        name="Acme Freight Co", domain="acme-freight-demo.com", website="https://acme-freight-demo.com",
        phone="+971 4 111", source="apify_maps")])
    run.pipeline.email_ok = email_ok
    return run


def contact_of(run):
    return next(c for c in run.repo.contacts.values() if c["status"] != "done")


def test_sourced_lead_gets_a_public_email_and_reaches_approval():
    run = sourced_lead_run(["careers@acme-freight-demo.com", "info@acme-freight-demo.com"])
    summary = run.pipeline.run(10)
    c = contact_of(run)
    assert c["email"] == "info@acme-freight-demo.com" and c["status"] == "awaiting_approval"
    assert summary.delivered == 1 and "Phone: +971 4 111" in run.notifier.sent[-1]["text"]


def test_no_usable_email_parks_the_lead_without_drafting():
    run = sourced_lead_run(["careers@acme-freight-demo.com"])
    summary = run.pipeline.run(10)
    c = contact_of(run)
    assert c["email"] is None and c["status"] == "nurture" and summary.no_email == 1
    assert not [x for x in run.llm.calls if x["task"] == "draft"]


def test_email_without_a_mail_server_or_suppressed_is_not_used():
    run = sourced_lead_run(["info@acme-freight-demo.com", "boss@gmail.com"],
                           email_ok=lambda e: not e.endswith("@acme-freight-demo.com"))
    run.repo.add_suppression("manual", email="boss@gmail.com")
    run.pipeline.run(10)
    assert contact_of(run)["status"] == "nurture"

"""The lead-source contract and the importer every source shares.

A source returns companies, not people: name, website, phone, category. Contact emails are found later
by the research agent on the company's own website (public business addresses only), so a source
never needs to buy or guess personal data.
"""
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from zypher_agent.db import Repository
from zypher_agent.models import CompanyIn, ContactIn


class SourcedCompany(BaseModel):
    name: str
    domain: str
    website: str
    phone: str | None = None
    address: str | None = None
    country: str | None = None
    industry: str | None = None
    rating: float | None = None
    reviews: int | None = None
    source: str  # e.g. "apify_maps"
    source_ref: str | None = None  # the provider's id for this place, for audit and de-duplication


class LeadSource(Protocol):
    def search(self, query: str, location: str, max_results: int) -> list[SourcedCompany]: ...


@dataclass
class ImportResult:
    imported: list[str] = field(default_factory=list)          # company names
    already_known: list[str] = field(default_factory=list)     # domain already in the database
    suppressed: list[str] = field(default_factory=list)        # domain on the do-not-contact list


def import_sourced(repo: Repository, companies: list[SourcedCompany]) -> ImportResult:
    """Add new companies as leads with no email yet. Known and suppressed domains are skipped.

    Re-running a search is therefore safe: nothing is duplicated and nothing re-enters the pipeline.
    """
    result = ImportResult()
    for c in companies:
        if repo.is_suppressed(None, c.domain):
            result.suppressed.append(c.name)
            continue
        if repo.company_exists(c.domain):
            result.already_known.append(c.name)
            continue
        notes = "; ".join(x for x in (
            f"Google rating {c.rating} ({c.reviews} reviews)" if c.rating is not None else None,
            c.address, f"ref {c.source_ref}" if c.source_ref else None,
        ) if x)
        repo.upsert_lead(
            CompanyIn(name=c.name, domain=c.domain, website=c.website, country=c.country,
                      industry=c.industry, notes=notes or None, source=c.source),
            ContactIn(phone=c.phone),
        )
        result.imported.append(c.name)
    return result

"""CSV lead import: parsing, validation, and writing through the Repository."""
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import tldextract
from email_validator import EmailNotValidError, validate_email

from zypher_agent.db import Repository
from zypher_agent.models import CompanyIn, ContactIn

# offline: no fetching the public-suffix list at import time
_extract = tldextract.TLDExtract(suffix_list_urls=())


def normalize_domain(website: str) -> str | None:
    website = (website or "").strip()
    if not website:
        return None
    ext = _extract(website)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}".lower()


def clean_email(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return validate_email(raw, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


# Addresses that reach the wrong person (or no one) for a sales email.
WRONG_INBOX = re.compile(
    r"^(no-?reply|do-?not-?reply|careers?|jobs?|hr|recruit\w*|resumes?|cv|privacy|abuse|postmaster|webmaster"
    r"|billing|accounts?payable|invoices?)@",
    re.IGNORECASE,
)


# Who reads an inbox decides whether a sales email gets answered. Best to worst; the first match wins.
INBOX_PREFERENCE = [
    re.compile(r"^(owner|founder|ceo|md|director|gm|manager|operations|ops)\b", re.IGNORECASE),   # a decision maker
    re.compile(r"^(sales|business|bd|enquir\w*|inquir\w*|quote\w*)\b", re.IGNORECASE),             # people who answer buyers
    re.compile(r"^(info|contact|hello|hi|office|admin|mail)\b", re.IGNORECASE),                     # the general inbox
]
SUPPORT_INBOX = re.compile(r"(support|help|care|service|tracking|complain\w*|feedback|customer|claims?)", re.IGNORECASE)


def _inbox_rank(email: str) -> int:
    local = email.split("@")[0]
    for i, pattern in enumerate(INBOX_PREFERENCE):
        if pattern.match(local):
            return i
    return 4 if SUPPORT_INBOX.search(local) else 3  # support desks answer shipment queries, not proposals


def rank_contact_emails(found: list[str], domain: str) -> list[str]:
    """Usable public addresses, best first: on the company's own domain, then by who reads the inbox.

    Addresses on other domains (many SMEs use gmail) come after every own-domain address.
    """
    usable = [e for e in dict.fromkeys(found) if not WRONG_INBOX.match(e)]
    own = [e for e in usable if e.endswith(("@" + domain, "." + domain))]
    other = [e for e in usable if e not in own]
    return sorted(own, key=_inbox_rank) + sorted(other, key=_inbox_rank)


def has_mail_server(email: str) -> bool:
    """True if the address's domain accepts mail (MX/A record). One DNS lookup, no email is sent."""
    try:
        validate_email(email, check_deliverability=True)
    except EmailNotValidError:
        return False
    return True


@dataclass
class ParsedCsv:
    rows: list[tuple[CompanyIn, ContactIn]] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)


def parse_csv(path: Path) -> ParsedCsv:
    """Columns: LeadID, CompanyName, Website, ContactName, Email, Country, Industry, Notes."""
    out, seen = ParsedCsv(), set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line, r in enumerate(csv.DictReader(f), start=2):
            domain = normalize_domain(r.get("Website", ""))
            email = clean_email(r.get("Email", ""))
            if not domain:
                out.skipped.append((line, "invalid or missing website"))
            elif not email:
                out.skipped.append((line, "invalid or missing email"))
            elif email in seen:
                out.skipped.append((line, "duplicate email in file"))
            else:
                seen.add(email)
                clean = lambda k, r=r: (r.get(k) or "").strip() or None
                out.rows.append((
                    CompanyIn(
                        name=clean("CompanyName") or domain, domain=domain, website=clean("Website"),
                        country=clean("Country"), industry=clean("Industry"), notes=clean("Notes"),
                        source="csv",
                    ),
                    ContactIn(full_name=clean("ContactName"), email=email, legacy_lead_id=clean("LeadID")),
                ))
    return out


def import_rows(repo: Repository, rows: list[tuple[CompanyIn, ContactIn]]) -> tuple[int, int]:
    """Write rows, skipping anyone on the suppression list or rejected before. Returns (imported, skipped)."""
    imported = suppressed = 0
    for company, contact in rows:
        if repo.is_suppressed(contact.email, company.domain) or repo.is_rejected(company.domain):
            suppressed += 1
            continue
        repo.upsert_lead(company, contact)
        imported += 1
    return imported, suppressed

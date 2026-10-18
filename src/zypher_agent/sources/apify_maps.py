"""Google Maps businesses via Apify's `compass/crawler-google-places` actor.

One synchronous API call per search. Cost control is enforced by Apify itself (`maxTotalChargeUsd`),
not just by our arithmetic. We ask only for places with a website and skip closed ones, and we do not
buy Apify's contact enrichment: our research agent reads public emails from the company's own site.
"""
from typing import Any

import httpx

from zypher_agent.leads import normalize_domain
from zypher_agent.sources.base import SourcedCompany

ACTOR = "compass~crawler-google-places"
RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
PRICE_PER_PLACE_USD = 0.004   # Apify's published rate for this actor; check the actor page if it changes
PRICE_PER_RUN_USD = 0.007
MIN_CHARGE_CAP_USD = 0.50     # Apify rejects a lower maxTotalChargeUsd
MAX_RESULTS = 100             # per search; a synchronous run must finish within Apify's 300 s limit

# A "website" on these domains is a profile page, not the company's own site: the domain is shared by
# thousands of businesses, so it can't identify the lead and there is nothing of theirs to research.
NOT_OWN_SITE = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com", "youtube.com",
    "wa.me", "whatsapp.com", "linktr.ee", "google.com", "business.site", "wixsite.com", "blogspot.com",
    "wordpress.com", "godaddysites.com", "square.site", "myshopify.com", "carrd.co", "bit.ly",
    # free app/site hosts (seen live: netlify.app, base44.app): every customer shares the same domain
    "netlify.app", "vercel.app", "base44.app", "github.io", "web.app", "firebaseapp.com", "pages.dev",
    "herokuapp.com", "webflow.io", "framer.website", "weebly.com", "jimdosite.com", "odoo.com", "zohosites.com",
    "lovable.app", "replit.app", "glide.page", "canva.site",
}
COUNTRY_NAMES = {"AE": "UAE", "IN": "India", "QA": "Qatar", "US": "US", "SA": "Saudi Arabia", "OM": "Oman"}


class SourceError(Exception):
    pass


def estimate_cost_usd(max_results: int) -> float:
    return round(PRICE_PER_RUN_USD + PRICE_PER_PLACE_USD * max_results, 3)


def parse_items(items: list[dict[str, Any]]) -> tuple[list[SourcedCompany], list[tuple[str, str]]]:
    """Turn actor output into companies. Returns (companies, [(name, reason skipped)])."""
    out: list[SourcedCompany] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for it in items:
        name = (it.get("title") or "").strip()
        website = (it.get("website") or "").strip()
        domain = normalize_domain(website) if website else None
        if not name:
            continue
        if it.get("permanentlyClosed") or it.get("temporarilyClosed"):
            skipped.append((name, "closed"))
        elif not domain:
            skipped.append((name, "no website"))
        elif domain in NOT_OWN_SITE:
            skipped.append((name, f"website is a {domain} page, not their own site"))
        elif domain in seen:
            skipped.append((name, "duplicate domain in results"))
        else:
            seen.add(domain)
            code = (it.get("countryCode") or "").upper() or None
            out.append(SourcedCompany(
                name=name, domain=domain, website=website, phone=it.get("phone") or None,
                address=it.get("address") or None, country=COUNTRY_NAMES.get(code or "", code),
                industry=it.get("categoryName") or None, rating=it.get("totalScore"),
                reviews=it.get("reviewsCount"), source="apify_maps", source_ref=it.get("placeId"),
            ))
    return out, skipped


class ApifyMapsSource:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self.client = client or httpx.Client(timeout=320.0)  # Apify allows a synchronous run up to 300 s
        self.last_skipped: list[tuple[str, str]] = []

    def __repr__(self) -> str:
        return "ApifyMapsSource(token=***)"

    def search(self, query: str, location: str, max_results: int) -> list[SourcedCompany]:
        if not 1 <= max_results <= MAX_RESULTS:
            raise SourceError(f"max_results must be 1-{MAX_RESULTS}")
        cap = max(MIN_CHARGE_CAP_USD, round(estimate_cost_usd(max_results) * 1.5, 2))
        try:
            r = self.client.post(
                RUN_URL,
                params={"maxTotalChargeUsd": cap, "timeout": 280},
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "searchStringsArray": [query], "locationQuery": location,
                    "maxCrawledPlacesPerSearch": max_results, "language": "en",
                    "website": "withWebsite", "skipClosedPlaces": True,
                },
            )
        except httpx.HTTPError as e:
            raise SourceError(f"Apify request failed: {type(e).__name__}") from None
        if r.status_code not in (200, 201):
            try:
                detail = r.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise SourceError(f"Apify returned HTTP {r.status_code}: {detail[:200]}")
        items = r.json()
        if not isinstance(items, list):
            raise SourceError("Apify returned an unexpected response shape")
        companies, self.last_skipped = parse_items(items)
        return companies

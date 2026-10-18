"""Fetch a lead's website safely and turn it into plain text for the research agent.

The website is untrusted input twice over: its text may try to instruct the LLM
(handled in the research agent), and its domain is chosen by whoever built the lead
list (handled here: we refuse to fetch private or internal addresses).

Everything that can be decided without a network (text extraction, link picking,
email finding, address checks) is a pure function, so it is unit-tested directly.
"""
import hashlib
import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Comment

from zypher_agent.leads import clean_email, normalize_domain

# Identify honestly as a bot. The CLI passes "<business> research bot (+<website>)" from config.
USER_AGENT = "SalesResearchBot/0.1"
MAX_BYTES = 1_000_000        # per page download
MAX_TEXT_CHARS = 8_000       # per page text handed to the LLM
MAX_REDIRECTS = 3
MAX_PAGES = 5                # homepage + one each of about, services, contact, careers
TIMEOUT_S = 10.0

# Page categories we try to find, in priority order. Matched against the URL path, then link text.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "about": ("about", "who-we-are", "our-story", "company"),
    "services": ("service", "solution", "what-we-do", "offering", "capabilit", "product"),
    "contact": ("contact", "enquir", "inquir", "get-in-touch", "reach-us", "quote"),
    "careers": ("career", "jobs", "join-us", "hiring", "vacanc"),
}
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|pptx?|mp4|mp3)$", re.IGNORECASE)
# Bounded quantifiers keep the scan linear: an unbounded `+` rescans a long run for every start position.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")
JUNK_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|js|css)$|@(example|domain|yourdomain|sentry|wixpress)\.", re.IGNORECASE)


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    text: str
    emails: list[str]
    fetched_at: str
    text_hash: str

    def meta(self) -> dict:
        """The shape stored in research_reports.pages_fetched."""
        return {"url": self.url, "fetched_at": self.fetched_at, "text_hash": self.text_hash}


@dataclass
class SiteFetch:
    pages: list[Page] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Fetcher(Protocol):
    def fetch_site(self, website: str) -> SiteFetch: ...


# ---------- pure helpers ----------

def resolve_host(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def is_public_host(host: str, resolver: Callable[[str], list[str]] = resolve_host) -> bool:
    """True only if every address the host resolves to is a public internet address."""
    try:
        addrs = resolver(host)
    except OSError:
        return False
    if not addrs:
        return False
    for raw in addrs:
        ip = ipaddress.ip_address(raw.split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return True


def extract_page(html: str, base_url: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Return (visible text, public emails, [(absolute link, anchor text)])."""
    soup = BeautifulSoup(html, "html.parser")
    emails: list[str] = []
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            emails.append(href[7:].split("?")[0])
        elif not href.lower().startswith(("tel:", "javascript:")):
            links.append((urldefrag(urljoin(base_url, href))[0], a.get_text(" ", strip=True)))

    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    # Text a human can't see is a classic place to hide instructions for an LLM.
    for tag in soup.find_all(attrs={"hidden": True}) + soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc = soup.find("meta", attrs={"name": "description"})
    parts = [title, desc.get("content", "") if desc else "", soup.get_text(" ", strip=True)]
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    emails += EMAIL.findall(text)
    return text[:MAX_TEXT_CHARS], emails, links


def clean_emails(raw: list[str], site_domain: str) -> list[str]:
    """Valid, de-duplicated addresses, the site's own domain first."""
    seen: dict[str, None] = {}
    for e in raw:
        e = clean_email(e) if not JUNK_EMAIL.search(e) else None
        if e:
            seen.setdefault(e, None)
    return sorted(seen, key=lambda e: (not e.endswith("@" + site_domain), e))


def pick_pages(home_url: str, links: list[tuple[str, str]]) -> list[str]:
    """Choose at most one same-site page per category (about, services, contact, careers)."""
    site = normalize_domain(home_url)
    chosen: list[str] = []
    for words in CATEGORIES.values():
        best: tuple[int, int, str] | None = None
        for url, anchor in links:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or normalize_domain(url) != site:
                continue
            if SKIP_EXT.search(parsed.path) or url in chosen or parsed.path in ("", "/"):
                continue
            path, text = parsed.path.lower(), anchor.lower()
            score = 2 if any(w in path for w in words) else 1 if any(w in text for w in words) else 0
            if score and (best is None or (-score, len(path)) < (-best[0], best[1])):
                best = (score, len(path), url)
        if best:
            chosen.append(best[2])
    return chosen[: MAX_PAGES - 1]


# ---------- network ----------

class HttpFetcher:
    def __init__(
        self,
        client: httpx.Client | None = None,
        resolver: Callable[[str], list[str]] = resolve_host,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self.resolver, self.user_agent = resolver, user_agent

    def _get(self, url: str, site: str) -> tuple[str, str]:
        """GET with manual redirects so every hop is checked. Returns (html, final_url)."""
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise FetchError(f"unsupported URL {url}")
            if normalize_domain(url) != site:
                raise FetchError(f"left the site: {url}")
            if not is_public_host(parsed.hostname, self.resolver):
                raise FetchError(f"non-public address: {parsed.hostname}")
            try:
                with self.client.stream("GET", url, headers={"User-Agent": self.user_agent}) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    if r.status_code != 200:
                        raise FetchError(f"HTTP {r.status_code} for {url}")
                    if "html" not in r.headers.get("content-type", "") and "text/" not in r.headers.get("content-type", ""):
                        raise FetchError(f"not a web page: {url}")
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) >= MAX_BYTES:
                            break
                    return bytes(body).decode(r.encoding or "utf-8", errors="replace"), url
            except httpx.HTTPError as e:
                raise FetchError(f"{type(e).__name__} for {url}") from e
        raise FetchError(f"too many redirects from {url}")

    def _allowed(self, robots: RobotFileParser | None, url: str) -> bool:
        return robots is None or robots.can_fetch(self.user_agent, url)

    def _robots(self, home_url: str, site: str) -> RobotFileParser | None:
        try:
            text, _ = self._get(urljoin(home_url, "/robots.txt"), site)
        except FetchError:
            return None  # no robots.txt (or unreadable): nothing forbids us
        rp = RobotFileParser()
        rp.parse(text.splitlines())
        return rp

    def fetch_site(self, website: str) -> SiteFetch:
        result = SiteFetch()
        site = normalize_domain(website)
        if not site:
            result.errors.append(f"invalid website {website!r}")
            return result
        explicit = "://" in website
        start = urljoin(website if explicit else f"https://{website}", urlparse(website if explicit else f"//{website}").path or "/")

        try:
            html, home_url = self._get(start, site)
        except FetchError as first:
            if explicit or "non-public" in str(first):
                result.errors.append(str(first))
                return result
            try:  # many small-business sites still only answer on http
                html, home_url = self._get(start.replace("https://", "http://", 1), site)
            except FetchError:
                result.errors.append(str(first))  # report the https failure, the primary attempt
                return result

        robots = self._robots(home_url, site)
        if not self._allowed(robots, home_url):
            result.errors.append("disallowed by robots.txt")
            return result

        text, raw_emails, links = extract_page(html, home_url)
        pages = [(home_url, text, raw_emails)]
        for url in pick_pages(home_url, links):
            if not self._allowed(robots, url):
                result.errors.append(f"disallowed by robots.txt: {url}")
                continue
            try:
                page_html, final = self._get(url, site)
            except FetchError as e:
                result.errors.append(str(e))
                continue
            t, e, _ = extract_page(page_html, final)
            pages.append((final, t, e))

        now = datetime.now(UTC).isoformat()
        for url, text, raw in pages:
            result.pages.append(Page(
                url=url, text=text, emails=clean_emails(raw, site), fetched_at=now,
                text_hash=hashlib.sha256(text.encode()).hexdigest(),
            ))
        return result


class FakeFetcher:
    """Canned sites for tests and --dry-run: {domain: SiteFetch}."""

    def __init__(self, sites: dict[str, SiteFetch]) -> None:
        self.sites = sites

    def fetch_site(self, website: str) -> SiteFetch:
        return self.sites.get(normalize_domain(website) or "", SiteFetch(errors=["site not found"]))

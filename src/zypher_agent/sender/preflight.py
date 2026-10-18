"""Checks to run BEFORE the first real send. Sends nothing.

Authentication records and mailbox logins are the most common reason cold email lands in spam or never
leaves, and both can be checked safely: DNS lookups and a login that is closed straight away.
"""
import imaplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import dns.exception
import dns.resolver

from zypher_agent.config import Settings
from zypher_agent.sender.transport import SendError, SMTPTransport

OK, WARN, FAIL = "ok", "warn", "fail"
DKIM_SELECTORS = ("default", "selector1", "selector2", "google", "s1", "s2", "k1", "mail", "dkim")


@dataclass
class Check:
    name: str
    status: str
    detail: str


def dns_txt(name: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=8)
    except (dns.exception.DNSException, OSError):
        return []
    return ["".join(part.decode() for part in r.strings) for r in answers]


def dns_mx(name: str) -> list[str]:
    try:
        return sorted(str(r.exchange).rstrip(".").lower() for r in dns.resolver.resolve(name, "MX", lifetime=8))
    except (dns.exception.DNSException, OSError):
        return []


def _registrable(host: str) -> str:
    parts = host.lower().rstrip(".").split(".")
    return ".".join(parts[-2:])


def check_dns(
    domain: str, smtp_host: str,
    txt: Callable[[str], list[str]] = dns_txt, mx: Callable[[str], list[str]] = dns_mx,
) -> list[Check]:
    out: list[Check] = []
    hosts = mx(domain)
    out.append(Check("MX records", OK if hosts else FAIL, ", ".join(hosts) or f"{domain} has no MX: it cannot receive replies"))

    spf = next((t for t in txt(domain) if t.lower().startswith("v=spf1")), None)
    provider = _registrable(smtp_host)
    if spf is None:
        out.append(Check("SPF", FAIL, f"no SPF record on {domain}: receivers cannot tell your mail is genuine"))
    elif provider not in spf.lower():
        out.append(Check("SPF", FAIL, f"SPF is '{spf}' and does not include {provider}, the server we send through: "
                                      "mail would fail SPF and be quarantined"))
    else:
        out.append(Check("SPF", OK, spf))

    dmarc = next((t for t in txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")), None)
    if dmarc is None:
        out.append(Check("DMARC", WARN, "no DMARC record: add one (start with p=none and a report address)"))
    elif "p=none" in dmarc.replace(" ", "").lower():
        out.append(Check("DMARC", WARN, f"{dmarc} (monitoring only: no protection against spoofing)"))
    else:
        out.append(Check("DMARC", OK, dmarc))

    found = [s for s in DKIM_SELECTORS if txt(f"{s}._domainkey.{domain}")]
    out.append(Check(
        "DKIM", OK if found else WARN,
        f"key found at selector {found[0]}" if found else
        "no key at common selectors. Some providers sign from another name, so this is not proof it is missing: "
        "send a test message to a Gmail address and look for 'dkim=pass' under 'Show original'",
    ))
    return out


def check_smtp(settings: Settings) -> Check:
    try:
        SMTPTransport(settings).check_login()
    except SendError as e:
        return Check("SMTP login", FAIL, str(e))
    return Check("SMTP login", OK, f"{settings.smtp_host}:{settings.smtp_port} as {settings.mail_user or settings.sender_email}")


def check_imap(settings: Settings) -> Check:
    if settings.mail_password is None:
        return Check("IMAP login", FAIL, "MAIL_PASSWORD is not set in .env")
    try:
        box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=20)
        box.login(settings.mail_user or settings.sender_email, settings.mail_password.get_secret_value())
        status, _ = box.select("INBOX", readonly=True)
        box.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        return Check("IMAP login", FAIL, f"{type(e).__name__}: check IMAP_HOST and the password")
    return Check("IMAP login", OK if status == "OK" else FAIL, f"{settings.imap_host}:{settings.imap_port}, INBOX readable")


def check_settings(settings: Settings, footer_problems: list[str]) -> list[Check]:
    out = []
    out.append(Check("Email footer", FAIL if footer_problems else OK, "; ".join(footer_problems) or "identity, opt-out line and postal address set"))
    try:
        ZoneInfo(settings.sender_timezone)
        out.append(Check("Timezone", OK, settings.sender_timezone))
    except Exception:  # noqa: BLE001 - any failure to load a zone means the setting is unusable
        out.append(Check("Timezone", FAIL, f"unknown timezone {settings.sender_timezone!r}"))
    out.append(Check("Daily cap", OK, f"{settings.daily_send_cap} a day (your cap; the warmup ramp still applies)"
                     if settings.daily_send_cap is not None else "warmup ramp: 5 a day, rising to 15"))
    return out


def run_preflight(settings: Settings, footer_problems: list[str]) -> list[Check]:
    domain = settings.sender_email.split("@")[-1] if "@" in settings.sender_email else ""
    checks = check_settings(settings, footer_problems)
    checks += check_dns(domain, settings.smtp_host) if domain else [Check("Sender address", FAIL, "SENDER_EMAIL is not set")]
    checks += [check_smtp(settings), check_imap(settings)]
    return checks

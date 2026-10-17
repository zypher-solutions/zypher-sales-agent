"""Deterministic draft checks. Pure functions: no LLM, no network, no database.

The LLM is never asked to police itself. Every rule here is plain code, so a
rule that passes in a test passes in production.
"""
import re

from zypher_agent.config import Icp
from zypher_agent.models import EmailDraft, Footer, GuardrailResult, Hypothesis, Violation

# icp.yaml `forbidden_proof` is prose, so the matchable phrases live here.
FORBIDDEN_PROOF_PHRASES = [
    "our clients", "our customers", "clients like", "customers like",
    "we have helped", "we've helped", "we helped", "proven results",
    "proven track record", "case study", "case studies", "trusted by",
]
# First touch pitches the workflow call only; building things comes after the call.
OFFER_SCOPE = re.compile(
    r"redesign|new website|website development|web development|app development"
    r"|build (you |your )?(a |an |the )?(new )?(website|site|app|mobile app)"
    r"|rebuild (your |the )?(website|site)",
    re.I,
)
PRICING = re.compile(r"\bpric(e|es|ing)\b|\bdiscount\b|[$₹€£]|\b(AED|QAR|USD|INR)\b", re.I)
HEDGES = re.compile(
    r"\?|\b(probably|likely|might|perhaps|wonder|wondering|curious|suspect|guess|imagine|maybe)\b",
    re.I,
)
CTA = re.compile(r"\b(15|20)\b.{0,12}min", re.I)
GREETING = re.compile(r"^\s*(hi|hello|dear|hey)\b", re.I)
SIGN_OFF = re.compile(r"\b(best regards|kind regards|regards|sincerely|best wishes|thanks,)\b", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def footer_problems(footer: Footer) -> list[str]:
    """Empty or placeholder footer fields. Checked up front so no money is spent on unsendable drafts."""
    return [
        f"{name} is empty or still a placeholder"
        for name, value in footer.model_dump().items()
        if not value.strip() or "todo" in value.lower()
    ]


def check_draft(
    draft: EmailDraft,
    hypotheses: list[Hypothesis],
    icp: Icp,
    footer: Footer,
    *,
    allow_proof: bool = False,
) -> GuardrailResult:
    """Check a draft against icp.yaml rules. `allow_proof=False` is the first-touch setting."""
    v: list[Violation] = []

    def fail(rule: str, detail: str) -> None:
        v.append(Violation(rule=rule, detail=detail))

    rules = icp.messaging_rules
    body, subject = draft.body, draft.subject
    text = f"{subject}\n{body}"
    lowered = text.lower()

    # length
    words = len(body.split())
    lo, hi = rules.email_word_range
    if not lo <= words <= hi:
        fail("word_count", f"{words} words, must be {lo}-{hi}")

    # banned phrases and unsupportable proof
    for phrase in rules.never_say:
        if phrase.lower() in lowered:
            fail("banned_phrase", phrase)
    for phrase in FORBIDDEN_PROOF_PHRASES:
        if phrase in lowered:
            fail("forbidden_proof", phrase)
    if not allow_proof and re.search(r"\busr\b", lowered):
        fail("proof_in_first_touch", "names USR; proof is omitted from the first touch")

    # numbers: only the CTA's, allowed metrics, and figures quoted from the lead's own site
    allowed = _numbers(str(icp.offer.get("primary_cta", "")))
    for p in icp.proof_points:
        for metric in p.allowed_metrics:
            allowed |= _numbers(metric)
    for h in hypotheses:
        allowed |= _numbers(h.quote)
    if "%" in text:
        fail("unsupported_number", "percentages are not allowed")
    for n in sorted(_numbers(text) - allowed):
        fail("unsupported_number", f"{n} is not in the CTA, allowed metrics, or cited evidence")

    # pricing is for the call, not the first touch
    if PRICING.search(text):
        fail("pricing", "no pricing in first touch")

    # first touch offers the workflow call, not a build
    if OFFER_SCOPE.search(text):
        fail("offer_scope", "first touch must not offer a website/app/software build")

    # evidence: every claim must point at stored evidence
    known = {h.id for h in hypotheses}
    proof_ids = {p.id for p in icp.proof_points}
    if not any(c.evidence_id in known for c in draft.claims):
        fail("no_evidence", "needs at least one claim backed by a research hypothesis")
    for c in draft.claims:
        if c.evidence_id in proof_ids and not allow_proof:
            fail("proof_in_first_touch", f"claim cites proof point {c.evidence_id}")
        elif c.evidence_id not in known | proof_ids:
            fail("unknown_evidence", f"claim cites {c.evidence_id!r}, which does not exist")

    # tone and structure
    if not CTA.search(body):
        fail("cta", "must ask for a 15-20 minute call")
    if not HEDGES.search(body):
        fail("hypothesis_style", "hypothesis must be phrased as a question or likelihood")
    if GREETING.search(body) or SIGN_OFF.search(body):
        fail("body_wrapper", "body must not contain a greeting or sign-off; code adds them")
    if not subject.strip() or len(subject) > 80:
        fail("subject", "subject must be 1-80 characters")
    if re.match(r"\s*(re|fwd?):", subject, re.I) or subject.lower().startswith("subject:"):
        fail("subject", "subject must not fake a reply/forward or repeat 'Subject:'")

    # footer: opt-out and sender identity are non-negotiable
    for problem in footer_problems(footer):
        fail("footer", problem)

    return GuardrailResult(violations=v)

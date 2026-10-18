"""Deterministic draft checks. Pure functions: no LLM, no network, no database.

The LLM is never asked to police itself. Every rule here is plain code, so a
rule that passes in a test passes in production.
"""
import re

from zypher_agent.config import Icp
from zypher_agent.models import (
    EmailDraft,
    Footer,
    GuardrailResult,
    Hypothesis,
    Violation,
)

# icp.yaml `forbidden_proof` is prose, so the matchable phrases live here.
FORBIDDEN_PROOF_PHRASES = [
    "our clients", "our customers", "clients like", "customers like",
    "we have helped", "we've helped", "we helped", "proven results",
    "proven track record", "case study", "case studies", "trusted by",
]
# A first touch asks a yes/no question, not for time. These phrases request a call, meeting or demo.
# ("no call needed" is fine: it never says "a call".)
CALL_ASK = re.compile(
    r"\b(hop|jump|get) on (a|an|the)\b"
    r"|\b(schedule|book|set up|arrange)\s+(a|an|the|some|our)?\s*(\w+\s+)?(call|chat|meeting|demo|time|slot)\b"
    r"|\b(a|an|the|our|short|quick|brief|free)\s+(\w+\s+)?(call|chat|meeting|demo|zoom|conversation)\b"
    r"|\b\d+[- ]?(min|minute)s?\b|\bcalendar\b|\bzoom\b|\bdemo\b",
    re.IGNORECASE,
)
LINK = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|in|ae|qa|co|io|net|org|ai|app)\b", re.IGNORECASE)
URGENCY = re.compile(
    r"\b(act now|limited time|last chance|hurry|urgent|urgently|spots? left|only today|expires?|"
    r"before it'?s too late|don'?t miss)\b",
    re.IGNORECASE,
)
# Words too common on any business website to show we read THEIRS. A draft must share at least
# MIN_SHARED_DETAIL other words (or numbers) with the quotes it cites, or it is the same email for everyone.
GENERIC_WORDS = set(
    "about their there which would could should these those where while other every using please contact quote "
    "quotes request requests form forms free customers customer client clients services service solutions solution "
    "company business team teams process manually manual email phone website online check great sorry hours today "
    "follow tools tool system systems existing sketch page pages enquiries enquiry details information offer "
    "modes provide provides quality support".split()
)
MIN_SHARED_DETAIL = 2
WORD = re.compile(r"[a-z]{5,}|\d{2,}")

# Result claims written in words instead of digits ("cut it in half", "double your replies") are still numbers.
RESULT_CLAIM = re.compile(
    r"\bin half\b|\bhalve[sd]?\b|\b(double|triple|quadruple)[sd]?\b|\b\d+x\b|\btenfold\b",
    re.IGNORECASE,
)
PRICING = re.compile(r"\bpric(e|es|ing)\b|\bdiscount\b|[$₹€£]|\b(AED|QAR|USD|INR)\b", re.IGNORECASE)
HEDGES = re.compile(
    r"\?|\b(probably|likely|might|perhaps|wonder|wondering|curious|suspect|guess|imagine|maybe)\b",
    re.IGNORECASE,
)
GREETING = re.compile(r"^\s*(hi|hello|dear|hey)\b", re.IGNORECASE)
SIGN_OFF = re.compile(r"\b(best regards|kind regards|regards|sincerely|best wishes|thanks,)\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def footer_problems(footer: Footer) -> list[str]:
    """Empty or placeholder footer fields. Checked up front so no money is spent on unsendable drafts."""
    return [
        f"{name} is empty or still a placeholder"
        for name, value in footer.model_dump(include=set(Footer.REQUIRED)).items()
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
    allowed = _numbers(str(icp.offer.get("first_touch_cta", "")))
    for p in icp.proof_points:
        for metric in p.allowed_metrics:
            allowed |= _numbers(metric)
    for h in hypotheses:
        allowed |= _numbers(h.quote)
    if "%" in text:
        fail("unsupported_number", "percentages are not allowed")
    if m := RESULT_CLAIM.search(text):
        fail("unsupported_number", f"{m.group(0)!r} is a result claim we cannot prove")
    for n in sorted(_numbers(text) - allowed):
        fail("unsupported_number", f"{n} is not in the CTA, allowed metrics, or cited evidence")

    # pricing is for the call, not the first touch
    if PRICING.search(text):
        fail("pricing", "no pricing in first touch")

    # first touch pitches only the first-touch offer; the others are raised on the call
    for offer in icp.offers:
        if offer.first_touch:
            continue
        for pattern in offer.avoid_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                fail("offer_scope", f"first touch must not pitch {offer.name!r}")
                break

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

    # specificity: the email must reuse concrete details from the evidence it cites, not just point at it
    cited = {c.evidence_id for c in draft.claims}
    quote_words = {
        w for h in hypotheses if h.id in cited for w in WORD.findall(h.quote.lower()) if w not in GENERIC_WORDS
    }
    shared = quote_words & {w for w in WORD.findall(body.lower()) if w not in GENERIC_WORDS}
    if quote_words and len(shared) < min(MIN_SHARED_DETAIL, len(quote_words)):  # never demand more than the quote has
        hints = ", ".join(sorted(quote_words, key=len, reverse=True)[:6])
        fail("specific_detail", f"mention a concrete detail from your cited evidence, such as: {hints}")

    # tone and structure
    stripped = body.rstrip()
    if not stripped.endswith("?"):
        fail("ends_with_question", "the email must end with one easy question")
    if stripped.count("?") > 2:
        fail("single_ask", "at most two questions: the observation and the ask")
    if CALL_ASK.search(body):
        fail("call_request", "do not ask for a call, meeting or time in a first touch; offer the sketch")
    if LINK.search(text):
        fail("links", "no links or web addresses in a first touch")
    if URGENCY.search(text):
        fail("urgency", "no urgency or scarcity language")
    if not HEDGES.search(body):
        fail("hypothesis_style", "hypothesis must be phrased as a question or likelihood")
    if GREETING.search(body) or SIGN_OFF.search(body):
        fail("body_wrapper", "body must not contain a greeting or sign-off; code adds them")
    if not subject.strip() or len(subject) > 80:
        fail("subject", "subject must be 1-80 characters")
    if len(subject.split()) > 6:
        fail("subject", "subject must be 6 words or fewer: short subjects read like a colleague, not a pitch")
    if re.match(r"\s*(re|fwd?):", subject, re.IGNORECASE) or subject.lower().startswith("subject:"):
        fail("subject", "subject must not fake a reply/forward or repeat 'Subject:'")

    # footer: opt-out and sender identity are non-negotiable
    for problem in footer_problems(footer):
        fail("footer", problem)

    return GuardrailResult(violations=v)

import pytest

from zypher_agent.config import Settings, load_icp
from zypher_agent.guardrails.checks import check_draft
from zypher_agent.models import Claim, EmailDraft, Footer, Hypothesis

ICP = load_icp(Settings(_env_file=None).icp_path)

GOOD_BODY = (
    "I noticed your site asks customers to email or WhatsApp you for every shipment quote. "
    "Are quotes still put together by hand from a spreadsheet each time? That usually eats "
    "staff time and invites small errors, especially when enquiries pile up. We help small "
    "businesses automate repetitive workflows like this, connecting WhatsApp, email and a "
    "simple CRM so replies go out faster. Would a free 15-minute call to walk through how "
    "your quotes work today be useful? No commitment, and I would only ask about your process."
)
HYP = Hypothesis(
    id="h1",
    hypothesis="Quotes are built manually from WhatsApp and email requests",
    quote="Request a quote by email or WhatsApp",
    source_url="https://example.com/contact",
    confidence="medium",
)
FOOTER = Footer(
    opt_out_line="If this is not relevant, reply STOP and I will not contact you again.",
    sender_name="Muhammed Hasheem",
    sender_title="CEO",
    sender_email="muhammedhasheem@zypher-solutions.com",
    company_name="Zypher Software Solutions LLP",
    postal_address="Zypher Software Solutions LLP, Kerala, India",
)


def make(**overrides) -> EmailDraft:
    data = dict(
        subject="Quotes by hand?",
        body=GOOD_BODY,
        claims=[Claim(text="quotes requested by email or WhatsApp", evidence_id="h1")],
    )
    return EmailDraft(**{**data, **overrides})


def rules(draft=None, footer=FOOTER, hyps=(HYP,), **kw) -> set[str]:
    result = check_draft(draft or make(), list(hyps), ICP, footer, **kw)
    return {v.rule for v in result.violations}


def test_good_draft_passes():
    assert 60 <= len(GOOD_BODY.split()) <= 100
    assert rules() == set()


def test_too_short_and_too_long():
    assert "word_count" in rules(make(body="Are quotes slow? Free 15 minute call?"))
    assert "word_count" in rules(make(body=GOOD_BODY + " word" * 40))


@pytest.mark.parametrize("phrase", ["revolutionary", "Game-Changing", "boost your digital presence"])
def test_banned_phrases_case_insensitive(phrase):
    assert "banned_phrase" in rules(make(body=GOOD_BODY + f" {phrase}"))


def test_banned_phrase_in_subject():
    assert "banned_phrase" in rules(make(subject="A revolutionary idea"))


@pytest.mark.parametrize("phrase", ["our clients", "proven results", "we've helped"])
def test_forbidden_proof(phrase):
    assert "forbidden_proof" in rules(make(body=GOOD_BODY + f" {phrase}"))


def test_usr_not_named_in_first_touch():
    assert "proof_in_first_touch" in rules(make(body=GOOD_BODY + " We built this for USR."))


def test_proof_claim_rejected_in_first_touch_allowed_later():
    claims = [Claim(text="x", evidence_id="h1"), Claim(text="y", evidence_id="usr_first_deployment")]
    assert "proof_in_first_touch" in rules(make(claims=claims))
    assert "proof_in_first_touch" not in rules(make(claims=claims), allow_proof=True)


def test_invented_numbers_and_percentages_rejected():
    assert "unsupported_number" in rules(make(body=GOOD_BODY + " Teams save 40 hours a week."))
    assert "unsupported_number" in rules(make(body=GOOD_BODY + " Cuts errors by half, 30%."))


def test_number_quoted_from_their_site_is_allowed():
    hyp = HYP.model_copy(update={"quote": "Serving 12 branches across the UAE"})
    body = GOOD_BODY.replace("every shipment quote", "12 branches")
    assert "unsupported_number" not in rules(make(body=body), hyps=[hyp])


@pytest.mark.parametrize("extra", ["Pricing starts low.", "It costs $500.", "AED 2000 only.", "20% discount."])
def test_pricing_rejected(extra):
    assert "pricing" in rules(make(body=GOOD_BODY + " " + extra))


def test_website_build_offer_rejected_but_mentioning_web_and_crm_is_fine():
    assert "offer_scope" in rules(make(body=GOOD_BODY + " We can redesign your website."))
    assert "offer_scope" in rules(make(body=GOOD_BODY + " Happy to build you a new website."))
    assert "offer_scope" not in rules(make(body=GOOD_BODY + " Your enquiry form feeds a CRM, right?"))


def test_claim_evidence_must_exist():
    assert "no_evidence" in rules(make(claims=[]))
    assert "unknown_evidence" in rules(make(claims=[Claim(text="x", evidence_id="h1"), Claim(text="y", evidence_id="h9")]))
    assert "no_evidence" in rules(make(claims=[Claim(text="x", evidence_id="h9")]))


def test_missing_cta():
    body = GOOD_BODY.replace("a free 15-minute call", "a conversation")
    assert "cta" in rules(make(body=body))


def test_hypothesis_must_be_hedged():
    flat = (
        "Your quotes are built by hand from a spreadsheet each time and that eats staff time "
        "and invites small errors whenever enquiries pile up across the business every week. "
        "We help small businesses automate repetitive workflows like this one by connecting "
        "WhatsApp, email and a simple CRM so replies go out faster to every customer. "
        "Let us schedule a free 15-minute call to walk through your process in detail. "
        "No commitment at all from your side."
    )
    assert "hypothesis_style" in rules(make(body=flat))


@pytest.mark.parametrize("wrapper", ["Hi John, ", "Dear team, ", "Best regards, Hasheem", "Sincerely, Hasheem"])
def test_greeting_and_signoff_not_in_body(wrapper):
    body = wrapper + GOOD_BODY if wrapper.startswith(("Hi", "Dear")) else GOOD_BODY + " " + wrapper
    assert "body_wrapper" in rules(make(body=body))


@pytest.mark.parametrize("subject", ["", "x" * 81, "Re: your quote", "Fwd: hello", "Subject: Quotes"])
def test_bad_subjects(subject):
    assert "subject" in rules(make(subject=subject))


def test_placeholder_or_empty_footer_rejected():
    assert "footer" in rules(footer=FOOTER.model_copy(update={"opt_out_line": "TODO: opt-out line"}))
    assert "footer" in rules(footer=FOOTER.model_copy(update={"postal_address": ""}))
    assert "footer" in rules(footer=FOOTER.model_copy(update={"sender_name": "  "}))

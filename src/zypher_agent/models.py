"""Domain models shared across modules. These are the contracts between agents."""
from datetime import datetime
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class Lead(BaseModel):
    """One contact joined with its company, as the pipeline sees it."""

    contact_id: str
    company_id: str
    company_name: str
    domain: str
    website: str | None = None
    country: str | None = None
    industry: str | None = None
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None


class Hypothesis(BaseModel):
    """A workflow-pain guess. `quote` must appear verbatim in the page it cites."""

    id: str  # h1, h2, h3 - assigned by code, referenced by draft claims
    hypothesis: str
    quote: str
    source_url: str
    confidence: Literal["low", "medium", "high"]


class Claim(BaseModel):
    """One factual statement in a draft and the evidence id that supports it."""

    text: str
    evidence_id: str  # a Hypothesis.id or a proof_points id


class EmailDraft(BaseModel):
    """LLM output for drafting. Subject and body are separate fields; code adds the footer."""

    subject: str
    body: str = Field(description="Main email text only: no greeting, sign-off or opt-out")
    claims: list[Claim]


class CompanyIn(BaseModel):
    name: str
    domain: str
    website: str | None = None
    country: str | None = None
    industry: str | None = None
    notes: str | None = None
    source: str = "csv"


class ContactIn(BaseModel):
    full_name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    legacy_lead_id: str | None = None


class ResearchReport(BaseModel):
    """Stored result of researching one company."""

    services: list[str] = []
    tech_signals: list[str] = []
    hypotheses: list[Hypothesis] = []
    summary: str = ""
    industry: str | None = None
    disqualify_reason: str | None = None  # set when the lead breaks an icp.yaml disqualifier
    decision_maker: str | None = None
    public_emails: list[str] = []
    pages_fetched: list[dict] = []  # [{url, fetched_at, text_hash}]
    model: str = ""


class NewMessage(BaseModel):
    contact_id: str
    subject: str
    body: str  # the full email as it would be sent: greeting + body + footer
    evidence: dict = {}  # hypotheses, claims and guardrail result used for this draft
    step: int = 1
    channel: str = "email"
    version: int = 1
    status: str = "draft"
    feedback: str | None = None


class Message(NewMessage):
    id: str
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    # sending (Phase 2)
    message_id_header: str | None = None
    in_reply_to: str | None = None
    send_after: datetime | None = None
    send_attempts: int = 0
    last_error: str | None = None
    sent_from: str | None = None
    sent_at: datetime | None = None


class SendStats(BaseModel):
    """What the send policy needs to know about sending so far."""

    sent_today: int = 0
    first_sent_at: datetime | None = None
    last_sent_at: datetime | None = None
    # sending (Phase 2)
    message_id_header: str | None = None
    in_reply_to: str | None = None
    send_after: datetime | None = None
    send_attempts: int = 0
    last_error: str | None = None
    sent_from: str | None = None
    sent_at: datetime | None = None


class SendStats(BaseModel):
    """What the send policy needs to know about sending so far."""

    sent_today: int = 0
    first_sent_at: datetime | None = None
    last_sent_at: datetime | None = None


class Footer(BaseModel):
    """Code-appended email ending. Never written by the LLM."""

    opt_out_line: str
    sender_name: str
    sender_title: str
    sender_email: str
    company_name: str
    postal_address: str
    # Optional contact lines under the signature. Not required by law, so not checked as missing.
    sender_phone: str = ""
    sender_website: str = ""

    REQUIRED: ClassVar[tuple[str, ...]] = ("opt_out_line", "sender_name", "sender_title", "sender_email", "company_name", "postal_address")


class Violation(BaseModel):
    rule: str
    detail: str


class GuardrailResult(BaseModel):
    violations: list[Violation] = []

    @property
    def passed(self) -> bool:
        return not self.violations

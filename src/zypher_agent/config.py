"""Configuration: secrets and runtime settings from env vars, business rules from icp.yaml."""
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# The single tenant until the roadmap says otherwise. Matches the row seeded by migration 002.
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class Settings(BaseSettings):
    """Everything that comes from the environment. Secrets are SecretStr so they never print."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True, env_ignore_empty=True)

    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"  # used when llm_provider == "anthropic"
    llm_max_tokens: int = 16000

    # Provider for all LLM calls. "openai_compat" covers Groq, Ollama, Together, Mistral, NIM...
    llm_provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    # Comma-separated chain: tried in order on rate limits or repeated invalid output.
    llm_models: str = "openai/gpt-oss-120b,openai/gpt-oss-20b"
    llm_reasoning_effort: str = "medium"  # low | medium | high (gpt-oss); "" to omit the parameter

    database_url: SecretStr | None = None
    tenant_id: str = DEFAULT_TENANT_ID

    apify_token: SecretStr | None = None  # lead sourcing (Google Maps via Apify)

    telegram_bot_token: SecretStr | None = None
    # NoDecode: take the raw "1,2,3" string and let the validator split it.
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = []
    # Where approval cards go. Unset: the first allowed user's private chat. Set it to a group chat id
    # (negative number) so partners in that group can review and approve too.
    telegram_approval_chat_id: int | None = None

    # Sender identity comes from .env (per business). Empty required values fail the footer check on purpose.
    sender_name: str = ""
    sender_title: str = ""
    sender_email: str = ""
    sender_phone: str = ""
    sender_website: str = ""
    sender_postal_address: str = "TODO: postal address"
    opt_out_line: str = "TODO: opt-out line"

    # Sending (Phase 2). Defaults are GoDaddy's mail servers, because that is where this domain's mail lives.
    # `preflight` logs in and checks them without sending anything. The password only ever lives in .env.
    mail_user: str = ""  # login name; empty means "same as sender_email"
    mail_password: SecretStr | None = None
    smtp_host: str = "smtpout.secureserver.net"
    smtp_port: int = 465  # 465 = encrypted from the first byte, 587 = upgraded after connecting
    imap_host: str = "imap.secureserver.net"
    imap_port: int = 993
    # Master switch. Off means the worker will not send anything, whatever else is configured.
    sending_enabled: bool = False
    # Staged rollout: when set (comma-separated addresses or domains), ONLY these recipients can receive email.
    # Use it for the first test sends to your own mailboxes, then clear it to go live.
    send_allowlist: str = ""
    sender_timezone: str = "Asia/Kolkata"  # where "today" starts and ends for the daily cap
    daily_send_cap: int | None = None  # your own cap; can only lower the built-in warmup ramp, never raise it

    icp_path: Path = REPO_ROOT / "icp.yaml"

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(p) for p in v.split(",") if p.strip()]
        return v


class Proof(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    text: str
    may_name_client: bool = False
    allowed_metrics: list[str] = []


class MessagingRules(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email_word_range: tuple[int, int]
    tone: str
    never_say: list[str]
    never_claim: list[str]
    hypothesis_style: str


class Offer(BaseModel):
    """One thing the business sells. Exactly one offer with first_touch=True is pitched in email 1."""

    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    first_touch: bool = False
    value_statement: str
    evidence_focus: str = "manual, repetitive workflows"  # what research looks for, for first-touch offers
    avoid_patterns: list[str] = []  # regexes rejected in a first touch (offers we are not pitching yet)


class Icp(BaseModel):
    """The parts of icp.yaml the agents and guardrails use."""

    model_config = ConfigDict(extra="ignore")
    version: int
    company: dict
    proof_points: list[Proof]
    forbidden_proof: list[str]
    messaging_rules: MessagingRules
    offer: dict
    strategy: dict
    target: dict
    offers: list[Offer] = []


def load_icp(path: Path) -> Icp:
    return Icp.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def first_touch_offer(icp: Icp) -> Offer:
    """The single offer pitched in a first touch. Config must define exactly one."""
    chosen = [o for o in icp.offers if o.first_touch]
    if len(chosen) != 1:
        raise ValueError(f"icp.yaml needs exactly one offer with first_touch: true, found {len(chosen)}")
    return chosen[0]

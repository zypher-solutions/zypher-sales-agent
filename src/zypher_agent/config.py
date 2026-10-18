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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"  # used when llm_provider == "anthropic"
    llm_max_tokens: int = 16000

    # Provider for all LLM calls. "openai_compat" covers Groq, Ollama, Together, Mistral, NIM...
    llm_provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    # Comma-separated chain: tried in order on rate limits or repeated invalid output.
    llm_models: str = "openai/gpt-oss-120b,qwen/qwen3.8-27b"
    llm_reasoning_effort: str = "medium"  # low | medium | high (gpt-oss); "" to omit the parameter

    database_url: SecretStr | None = None
    tenant_id: str = DEFAULT_TENANT_ID

    telegram_bot_token: SecretStr | None = None
    # NoDecode: take the raw "1,2,3" string and let the validator split it.
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = []

    sender_name: str = "Muhammed Hasheem"
    sender_title: str = "Chief Executive Officer"
    sender_email: str = ""
    sender_postal_address: str = "TODO: postal address"
    opt_out_line: str = "TODO: opt-out line"

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


def load_icp(path: Path) -> Icp:
    return Icp.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

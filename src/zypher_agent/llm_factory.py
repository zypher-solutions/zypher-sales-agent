"""Pick the LLM provider from settings. The rest of the code only sees the `LLM` protocol."""
from zypher_agent.config import Settings
from zypher_agent.db import Repository
from zypher_agent.llm import LLM, ClaudeLLM, LLMError


def build_llm(settings: Settings, repo: Repository) -> LLM:
    if settings.llm_provider == "anthropic":
        return ClaudeLLM(settings, repo)
    if settings.llm_provider == "openai_compat":
        from zypher_agent.llm_openai import OpenAICompatLLM

        return OpenAICompatLLM(settings, repo)
    raise LLMError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")

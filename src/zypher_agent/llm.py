"""The only place the agent talks to an LLM.

Every call goes through `LLM.call`, which takes a Pydantic model as the output
contract and returns a validated instance. No free-form text ever comes back, so
there is nothing to strip or regex-repair. Every call is logged to `events` with
its inputs, outputs, model and cost.
"""
import time
from typing import Callable, Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from zypher_agent.config import Settings
from zypher_agent.db import Repository

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output). Prompt caching is not used, so no cache rates.
# Thinking tokens are billed as output and already included in usage.output_tokens.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class LLMError(Exception):
    """The call did not produce a usable structured result."""


class LLMRefused(LLMError):
    """The model declined (stop_reason == 'refusal'). The lead goes to a human."""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """None for a model we have no price for: the call is still logged, just uncosted."""
    price = PRICES.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)


class LLM(Protocol):
    model: str

    def call(
        self, *, task: str, system: str, user: str, output: type[T], contact_id: str | None = None
    ) -> T: ...


def _record(
    repo: Repository, *, task: str, model: str, system: str, user: str, output: BaseModel | None,
    contact_id: str | None, input_tokens: int, output_tokens: int, cost: float | None,
    request_id: str | None, seconds: float, error: str | None = None,
) -> None:
    repo.log_event(
        "llm_call",
        contact_id=contact_id,
        payload={
            "task": task, "model": model, "system": system, "user": user,
            "output": output.model_dump() if output else None, "error": error,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "request_id": request_id, "seconds": round(seconds, 2),
        },
    )


class ClaudeLLM:
    def __init__(
        self, settings: Settings, repo: Repository, client: anthropic.Anthropic | None = None
    ) -> None:
        if client is None:
            if settings.anthropic_api_key is None:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
        self.client, self.repo = client, repo
        self.model, self.max_tokens = settings.llm_model, settings.llm_max_tokens

    def call(
        self, *, task: str, system: str, user: str, output: type[T], contact_id: str | None = None
    ) -> T:
        start = time.monotonic()
        # output_format makes the API constrain generation to the schema, then the SDK
        # validates it into the Pydantic model. (Forced tool_choice is a 400 on Sonnet 5.5.)
        resp = self.client.messages.parse(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}], output_format=output,
        )
        parsed = resp.parsed_output if resp.stop_reason == "end_turn" else None
        in_tok, out_tok = resp.usage.input_tokens, resp.usage.output_tokens
        error = None if parsed is not None else f"stop_reason={resp.stop_reason}"
        # Log before raising: a refused or truncated call still cost money.
        _record(
            self.repo, task=task, model=self.model, system=system, user=user, output=parsed,
            contact_id=contact_id, input_tokens=in_tok, output_tokens=out_tok,
            cost=cost_usd(self.model, in_tok, out_tok), request_id=getattr(resp, "_request_id", None),
            seconds=time.monotonic() - start, error=error,
        )
        if resp.stop_reason == "refusal":
            raise LLMRefused(f"{task}: model refused")
        if parsed is None:
            raise LLMError(f"{task}: no structured output ({error})")
        return parsed


class MockLLM:
    """Canned answers keyed by task name. Used by --dry-run and tests: free, offline, deterministic.

    A response is a model instance, or a function (system, user) -> model instance.
    """

    def __init__(
        self, repo: Repository,
        responses: dict[str, BaseModel | Callable[[str, str], BaseModel]],
    ) -> None:
        self.repo, self.responses = repo, responses
        self.model = "mock"
        self.calls: list[dict] = []

    def call(
        self, *, task: str, system: str, user: str, output: type[T], contact_id: str | None = None
    ) -> T:
        if task not in self.responses:
            raise LLMError(f"MockLLM has no response for task {task!r}")
        r = self.responses[task]
        result = r(system, user) if callable(r) else r
        if not isinstance(result, output):
            raise LLMError(f"MockLLM response for {task!r} is not a {output.__name__}")
        self.calls.append({"task": task, "system": system, "user": user})
        _record(
            self.repo, task=task, model="mock", system=system, user=user, output=result,
            contact_id=contact_id, input_tokens=0, output_tokens=0, cost=0.0,
            request_id=None, seconds=0.0,
        )
        return result

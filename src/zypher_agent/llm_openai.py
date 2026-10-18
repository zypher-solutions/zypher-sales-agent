"""LLM provider for any OpenAI-compatible chat API (Groq, Ollama, Together, Mistral, NVIDIA NIM...).

Same contract as ClaudeLLM: `call(task, system, user, output)` returns a validated Pydantic
object and logs every attempt. What is different here, and why:

- Structured output: we send the Pydantic model's JSON schema as `response_format` with
  `strict: true`, so the provider constrains decoding to the schema. Strict mode has rules
  (every property required, additionalProperties false), which `strict_schema` applies.
  Constraints the schema can no longer express (lengths, ranges) are still enforced when
  we validate the reply with Pydantic.
- A model chain: free tiers limit requests/tokens per model. On a rate limit or a model that
  keeps failing validation we move to the next model in the chain, which also multiplies the
  free budget.
- One corrective retry: if a reply fails validation we ask again with the validation errors
  (structured feedback, never regex repair of the model's text).
"""
import copy
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from zypher_agent.config import Settings
from zypher_agent.db import Repository
from zypher_agent.llm import LLMError, _record, cost_usd

T = TypeVar("T", bound=BaseModel)

# JSON Schema keywords strict mode may reject. Pydantic re-checks them after parsing.
_UNSUPPORTED = {
    "default", "format", "pattern", "minLength", "maxLength", "minItems", "maxItems",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "examples", "uniqueItems",
}
MAX_RETRY_AFTER = 60.0  # seconds we wait on a 429 before moving to the next model (free tiers reset per minute)


def strict_schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema, rewritten to satisfy strict structured-output rules."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in _UNSUPPORTED & node.keys():
                del node[key]
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


class OpenAICompatLLM:
    def __init__(
        self, settings: Settings, repo: Repository, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.llm_api_key is None:
            raise LLMError("LLM_API_KEY (or GROQ_API_KEY) is not set")
        self.models = [m.strip() for m in settings.llm_models.split(",") if m.strip()]
        if not self.models:
            raise LLMError("LLM_MODELS is empty")
        self.model = self.models[0]  # the preferred model; each logged call records the one actually used
        self.repo, self.sleep = repo, sleep
        self.max_tokens, self.effort = settings.llm_max_tokens, settings.llm_reasoning_effort
        self.client = client or httpx.Client(timeout=120.0)
        self.url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self._key = settings.llm_api_key.get_secret_value()

    def call(
        self, *, task: str, system: str, user: str, output: type[T], contact_id: str | None = None
    ) -> T:
        schema = strict_schema(output)
        errors: list[str] = []
        for model in self.models:
            prompt = user
            for attempt in range(2):
                result, error, retry_feedback = self._attempt(
                    model, task, system, prompt, output, schema, contact_id
                )
                if result is not None:
                    return result
                errors.append(f"{model}: {error}")
                if retry_feedback is None:  # not a validation problem: go to the next model
                    break
                prompt = f"{user}\n\nYour previous reply was rejected: {retry_feedback}\nReturn corrected JSON."
        raise LLMError(f"{task}: no usable structured output. " + " | ".join(errors))

    def _attempt(
        self, model: str, task: str, system: str, user: str, output: type[T], schema: dict,
        contact_id: str | None,
    ) -> tuple[T | None, str | None, str | None]:
        """Returns (result, error, validation_feedback). feedback is set only when a retry could help."""
        body: dict[str, Any] = {
            "model": model, "temperature": 0.2, "max_completion_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": output.__name__, "strict": True, "schema": schema},
            },
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
        if model.startswith("openai/gpt-oss"):
            body["include_reasoning"] = False  # keep the reply small; reasoning is not needed downstream
        start = time.monotonic()
        resp = self._post(body)
        usage = (resp.json().get("usage") or {}) if resp.status_code == 200 else {}
        in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

        parsed, error, feedback = None, None, None
        if resp.status_code != 200:
            error = f"http {resp.status_code}: {resp.text[:200]}"
            if resp.status_code == 400 and "does not match the expected schema" in resp.text:
                feedback = "the JSON did not match the schema"  # best-effort models can fail here; retry once
        else:
            choice = resp.json()["choices"][0]
            content = choice["message"].get("content") or ""
            if choice.get("finish_reason") == "length":
                error = "truncated (finish_reason=length); raise LLM_MAX_TOKENS or lower reasoning effort"
            else:
                try:
                    parsed = output.model_validate_json(content)
                except ValidationError as e:
                    error = f"validation failed: {e.error_count()} error(s)"
                    feedback = "; ".join(
                        f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5]
                    )
        _record(
            self.repo, task=task, model=model, system=system, user=user, output=parsed,
            contact_id=contact_id, input_tokens=in_tok, output_tokens=out_tok,
            cost=cost_usd(model, in_tok, out_tok), request_id=resp.headers.get("x-request-id"),
            seconds=time.monotonic() - start, error=error,
        )
        return parsed, error, feedback

    def _post(self, body: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._key}"}
        resp = self.client.post(self.url, json=body, headers=headers)
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("retry-after", "inf"))
            except ValueError:
                wait = float("inf")
            if wait <= MAX_RETRY_AFTER:
                self.sleep(wait)
                resp = self.client.post(self.url, json=body, headers=headers)
        return resp

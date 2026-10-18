import json
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel, Field, SecretStr

from zypher_agent.config import Settings
from zypher_agent.db import InMemoryRepo
from zypher_agent.llm import LLMError
from zypher_agent.llm_factory import build_llm
from zypher_agent.llm_openai import OpenAICompatLLM, strict_schema

SECRET = "gsk_test_secret_value"


class Inner(BaseModel):
    note: str = Field(max_length=50)
    level: Literal["low", "high"]


class Out(BaseModel):
    answer: str
    maybe: str | None = None
    count: int = Field(default=0, ge=0)
    items: list[Inner] = []


def settings(**kw):
    base = {"_env_file": None, "llm_api_key": SecretStr(SECRET), "llm_models": "m-a,m-b", "llm_reasoning_effort": "low"}
    return Settings(**{**base, **kw})


def reply(content: str, finish="stop", tokens=(10, 5)) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]},
    })


def make(responses, **kw):
    """LLM whose HTTP layer returns `responses` in order; records the requests."""
    seen, queue = [], list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content) | {"_auth": request.headers["authorization"]})
        return queue.pop(0)

    repo = InMemoryRepo()
    sleeps: list[float] = []
    llm = OpenAICompatLLM(
        settings(**kw), repo, httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append
    )
    return llm, repo, seen, sleeps


def call(llm):
    return llm.call(task="t", system="sys", user="usr", output=Out)


def test_strict_schema_rules():
    s = strict_schema(Out)
    assert s["additionalProperties"] is False
    assert s["required"] == ["answer", "maybe", "count", "items"]  # strict: every property required
    inner = s["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["note", "level"]
    text = json.dumps(s)
    assert '"default"' not in text and "maxLength" not in text and '"minimum"' not in text
    assert "null" in json.dumps(s["properties"]["maybe"])  # optional stays nullable


def test_success_sends_strict_schema_and_logs():
    llm, repo, seen, _ = make([reply('{"answer":"hi","maybe":null,"count":1,"items":[]}')])
    out = call(llm)
    assert out.answer == "hi" and out.count == 1
    req = seen[0]
    assert req["model"] == "m-a" and req["reasoning_effort"] == "low"
    assert req["response_format"]["json_schema"]["strict"] is True
    assert req["messages"][0] == {"role": "system", "content": "sys"}
    ev = [e for e in repo.events if e["type"] == "llm_call"]
    assert len(ev) == 1 and ev[0]["payload"]["model"] == "m-a" and ev[0]["payload"]["input_tokens"] == 10


def test_api_key_is_sent_but_never_logged():
    llm, repo, seen, _ = make([reply('{"answer":"x","maybe":null,"count":0,"items":[]}')])
    call(llm)
    assert seen[0]["_auth"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(repo.events, default=str)


def test_gpt_oss_excludes_reasoning_from_reply():
    llm, _, seen, _ = make([reply('{"answer":"x","maybe":null,"count":0,"items":[]}')], llm_models="openai/gpt-oss-120b")
    call(llm)
    assert seen[0]["include_reasoning"] is False


def test_validation_failure_retries_once_with_feedback():
    bad = reply('{"answer":"x","maybe":null,"count":-1,"items":[]}')  # count violates ge=0
    good = reply('{"answer":"fixed","maybe":null,"count":0,"items":[]}')
    llm, repo, seen, _ = make([bad, good])
    assert call(llm).answer == "fixed"
    assert len(seen) == 2 and seen[1]["model"] == "m-a"  # same model, second try
    assert "count" in seen[1]["messages"][1]["content"] and "rejected" in seen[1]["messages"][1]["content"]
    errors = [e["payload"]["error"] for e in repo.events]
    assert errors[0].startswith("validation failed") and errors[1] is None


def test_rate_limit_falls_back_to_next_model():
    limited = httpx.Response(429, headers={"retry-after": "3600"}, text="rate limited")
    good = reply('{"answer":"from b","maybe":null,"count":0,"items":[]}')
    llm, _, seen, sleeps = make([limited, good])
    assert call(llm).answer == "from b"
    assert [r["model"] for r in seen] == ["m-a", "m-b"] and sleeps == []  # too long to wait: switched


def test_short_rate_limit_waits_then_retries_same_model():
    limited = httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
    good = reply('{"answer":"ok","maybe":null,"count":0,"items":[]}')
    llm, _, seen, sleeps = make([limited, good])
    assert call(llm).answer == "ok"
    assert [r["model"] for r in seen] == ["m-a", "m-a"] and sleeps == [2.0]


def test_all_models_failing_raises_and_logs_every_attempt():
    fail = lambda: httpx.Response(500, text="boom")
    llm, repo, _, _ = make([fail(), fail()])
    with pytest.raises(LLMError, match="m-a.*m-b"):
        call(llm)
    assert len([e for e in repo.events if e["type"] == "llm_call"]) == 2


def test_truncated_reply_is_an_error_not_parsed():
    llm, _, _, _ = make([reply('{"answer":"cut', finish="length"), reply('{"answer":"cut', finish="length")])
    with pytest.raises(LLMError, match="truncated"):
        call(llm)


def test_missing_key_and_factory():
    with pytest.raises(LLMError):
        OpenAICompatLLM(Settings(_env_file=None, llm_api_key=None), InMemoryRepo())
    assert isinstance(build_llm(settings(), InMemoryRepo()), OpenAICompatLLM)
    with pytest.raises(LLMError):
        OpenAICompatLLM(settings(llm_models=" , "), InMemoryRepo())


def test_groq_key_alias(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_alias")
    assert Settings(_env_file=None).llm_api_key.get_secret_value() == "gsk_alias"

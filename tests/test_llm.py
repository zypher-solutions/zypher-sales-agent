from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from zypher_agent.config import Settings
from zypher_agent.db import InMemoryRepo
from zypher_agent.llm import ClaudeLLM, LLMError, LLMRefused, MockLLM, cost_usd


class Out(BaseModel):
    answer: str


class FakeMessages:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def fake_client(parsed=None, stop_reason="end_turn", tokens=(1000, 500)):
    resp = SimpleNamespace(
        parsed_output=parsed, stop_reason=stop_reason, _request_id="req_1",
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )
    return SimpleNamespace(messages=FakeMessages(resp))


def settings():
    return Settings(_env_file=None, llm_model="claude-sonnet-5-5")


def test_cost_known_and_unknown_model():
    # 1M in at $2 + 1M out at $10
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == 12.0
    assert cost_usd("claude-sonnet-5-5", 1000, 500) == 0.007
    assert cost_usd("some-future-model", 1, 1) is None


def test_claude_call_returns_parsed_model_and_logs_everything():
    repo, client = InMemoryRepo(), fake_client(parsed=Out(answer="hi"))
    out = ClaudeLLM(settings(), repo, client).call(
        task="t", system="SYS", user="USR", output=Out, contact_id="c1"
    )
    assert out == Out(answer="hi")
    sent = client.messages.kwargs
    assert sent["model"] == "claude-sonnet-5-5" and sent["output_format"] is Out
    assert sent["system"] == "SYS" and sent["messages"] == [{"role": "user", "content": "USR"}]
    (event,) = repo.events
    p = event["payload"]
    assert event["type"] == "llm_call" and event["contact_id"] == "c1"
    assert p["task"] == "t" and p["system"] == "SYS" and p["user"] == "USR"
    assert p["output"] == {"answer": "hi"} and p["cost_usd"] == 0.007 and p["request_id"] == "req_1"


def test_refusal_is_logged_then_raised():
    repo = InMemoryRepo()
    llm = ClaudeLLM(settings(), repo, fake_client(stop_reason="refusal"))
    with pytest.raises(LLMRefused):
        llm.call(task="t", system="s", user="u", output=Out)
    assert repo.events[0]["payload"]["error"] == "stop_reason=refusal"
    assert repo.events[0]["payload"]["cost_usd"] > 0  # the failed call is still costed


def test_truncated_output_raises():
    llm = ClaudeLLM(settings(), InMemoryRepo(), fake_client(stop_reason="max_tokens"))
    with pytest.raises(LLMError):
        llm.call(task="t", system="s", user="u", output=Out)


def test_missing_api_key_fails_clearly():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        ClaudeLLM(settings(), InMemoryRepo())


def test_mock_returns_canned_and_logs_zero_cost():
    repo = InMemoryRepo()
    mock = MockLLM(repo, {"t": Out(answer="canned"), "f": lambda s, u: Out(answer=u.upper())})
    assert mock.call(task="t", system="s", user="u", output=Out).answer == "canned"
    assert mock.call(task="f", system="s", user="abc", output=Out).answer == "ABC"
    assert [e["payload"]["cost_usd"] for e in repo.events] == [0.0, 0.0]
    assert mock.calls[1]["user"] == "abc"


def test_mock_rejects_unknown_task_and_wrong_type():
    class Other(BaseModel):
        x: int = 1

    mock = MockLLM(InMemoryRepo(), {"t": Out(answer="a")})
    with pytest.raises(LLMError):
        mock.call(task="missing", system="s", user="u", output=Out)
    with pytest.raises(LLMError):
        mock.call(task="t", system="s", user="u", output=Other)

import json

import httpx
import pytest

from zypher_agent.approval import Button
from zypher_agent.integrations.telegram import (
    TelegramClient,
    TelegramError,
    poll_forever,
    poll_once,
)

TOKEN = "123456:SECRET-TOKEN"


def client(handler):
    return TelegramClient(TOKEN, httpx.Client(transport=httpx.MockTransport(handler)))


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_send_builds_inline_keyboard_and_returns_message_id():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return ok({"message_id": 42})

    tg = client(handler)
    assert tg.send(7, "hello", [Button(text="Go", data="a|1"), Button(text="No", data="b|1")]) == 42
    assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert seen["body"]["reply_markup"]["inline_keyboard"] == [
        [{"text": "Go", "callback_data": "a|1"}, {"text": "No", "callback_data": "b|1"}]
    ]


def test_send_without_buttons_has_no_markup():
    seen = {}
    tg = client(lambda req: (seen.update(json.loads(req.content)) or ok({"message_id": 1})))
    tg.send(7, "plain")
    assert "reply_markup" not in seen


def test_api_errors_raise_without_leaking_the_token():
    tg = client(lambda req: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramError, match="Unauthorized"):
        tg.send(7, "x")

    def explode(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    with pytest.raises(TelegramError) as e:
        client(explode).send(7, "x")
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value) and e.value.__cause__ is None
    assert TOKEN not in repr(client(explode))


def test_edit_tolerates_not_modified_but_not_other_errors():
    tg = client(lambda req: httpx.Response(400, json={"ok": False, "description": "Bad Request: message is not modified"}))
    tg.edit(7, 1, "same")
    tg = client(lambda req: httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(TelegramError):
        tg.edit(7, 1, "x")


class FakeService:
    def __init__(self):
        self.handled = []
        self.repo = type("R", (), {"events": [], "log_event": lambda s, *a, **k: s.events.append((a, k))})()

    def handle_update(self, update):
        self.handled.append(update)


def test_poll_once_dispatches_and_advances_offset():
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return ok([{"update_id": 10, "message": {}}, {"update_id": 11, "message": {}}])

    service = FakeService()
    offset = poll_once(client(handler), service, None)
    assert offset == 12 and [u["update_id"] for u in service.handled] == [10, 11]
    assert "offset" not in requests[0] and requests[0]["allowed_updates"] == ["callback_query", "message"]
    poll_once(client(handler), service, offset)
    assert len(requests) == 2 and requests[1]["offset"] == 12  # the next poll acknowledges the first batch


def test_poll_forever_survives_network_errors_and_backs_off():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        raise KeyboardInterrupt  # stop the loop in the test

    sleeps = []
    service = FakeService()
    with pytest.raises(KeyboardInterrupt):
        poll_forever(client(handler), service, sleep=sleeps.append)
    assert sleeps == [5] and service.repo.events[0][0][0] == "telegram_error"


def test_two_pollers_are_reported_once_and_listening_continues():
    # Telegram answers 409 when a second copy (laptop and server) polls the same bot: taps go to either at random.
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(409, json={"ok": False, "error_code": 409, "description":
                                             "Conflict: terminated by other getUpdates request"})
        raise KeyboardInterrupt

    alerts, beats, service = [], [], FakeService()
    with pytest.raises(KeyboardInterrupt):
        poll_forever(client(handler), service, sleep=lambda s: None, on_conflict=alerts.append,
                     beat=lambda: beats.append(1))
    assert len(alerts) == 1 and "another copy" in alerts[0].lower()
    assert [e[0][0] for e in service.repo.events] == ["telegram_conflict"] * 3
    assert len(beats) >= 3  # the heartbeat means "the loop is alive", even while Telegram refuses us


def test_the_slash_menu_lists_every_command_the_bot_handles():
    import json
    import re

    import httpx

    from zypher_agent.integrations.telegram import COMMANDS, TelegramClient

    seen = []

    def handler(request):
        seen.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    TelegramClient("123:abc", httpx.Client(transport=httpx.MockTransport(handler))).set_commands()
    method, body = seen[0]
    names = [c["command"] for c in body["commands"]]
    assert method == "setMyCommands" and names == [c for c, _ in COMMANDS]
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", n) and 3 <= len(c["description"]) <= 256 for n, c in zip(names, body["commands"], strict=True))
    # every command in the menu must be one the bot actually handles
    src = open("src/zypher_agent/approval.py", encoding="utf-8").read()
    assert all(f'"/{n}"' in src for n in names)

"""Telegram Bot API over plain HTTPS (httpx). No Telegram library: three calls and a poll loop.

Polling (getUpdates) means no public URL, no webhook and no server to expose. Telegram holds
undelivered button taps for a while, and all state lives in the database, so restarting the
bot loses nothing.
"""
import time
from collections.abc import Callable

import httpx

from zypher_agent.approval import ApprovalService, Button

API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self.client = client or httpx.Client(timeout=40.0)  # longer than the 30s long-poll

    def __repr__(self) -> str:
        return "TelegramClient(token=***)"

    def _call(self, method: str, payload: dict) -> object:
        try:
            r = self.client.post(f"{API}/bot{self._token}/{method}", json=payload)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            # `from None`: the original exception text contains the URL, and the URL contains the token.
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'unknown error')}")
        return data["result"]

    def send(self, chat_id: int, text: str, buttons: list[Button] | None = None) -> int:
        payload: dict = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": b.text, "callback_data": b.data} for b in buttons]]
            }
        return self._call("sendMessage", payload)["message_id"]

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:  # no reply_markup in the payload: Telegram removes the buttons
            self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def answer(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload)


def poll_once(tg: TelegramClient, service: ApprovalService, offset: int | None, timeout: int = 30) -> int | None:
    """Fetch one batch of updates and handle each. Returns the next offset (acknowledges the batch)."""
    for update in tg.get_updates(offset, timeout):
        offset = update["update_id"] + 1
        service.handle_update(update)
    try:  # you cleared a card: show the next queued one
        service.release_queue()
    except Exception as e:  # noqa: BLE001 - a queue hiccup must not stop the bot from listening
        service.repo.log_event("queue_error", payload={"error": f"{type(e).__name__}: {e}"})
    return offset


def poll_forever(tg: TelegramClient, service: ApprovalService, sleep: Callable[[float], None] = time.sleep) -> None:
    offset = None
    while True:
        try:
            offset = poll_once(tg, service, offset)
        except Exception as e:  # network, Telegram or database trouble: back off and keep listening
            try:
                service.repo.log_event("telegram_error", payload={"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass  # the database itself may be what is down
            sleep(5)

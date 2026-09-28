"""The HTTPS client of the Telegram Bot API: one method per call the bot makes.

It knows the Bot API and nothing of edarunner. Every call goes through `BotApi.call`,
so a test replaces that one method.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import uuid
from typing import Any
from urllib.request import Request, urlopen

# Only an idempotent call is sent again after a network failure; a resent sendMessage posts twice.
RETRIES = {"getUpdates", "editMessageText", "answerCallbackQuery"}


class ApiError(Exception):
    """Telegram refused a call, or the network failed after the retries."""


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, data) in files.items():
        head = f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
        out += (head + "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


class BotApi:
    """One bot token. A method returns the `result` of its call or raises ApiError."""

    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, params: dict[str, Any], files: dict[str, tuple[str, bytes]] | None = None) -> Any:
        """One Bot API call. Obeys a 429; retries a network failure twice for a method in RETRIES."""
        fields = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items() if v is not None}
        if files:
            body, ctype = _multipart(fields, files)
        else:
            body, ctype = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
        req = Request(f"https://api.telegram.org/bot{self.token}/{method}", body, {"Content-Type": ctype})
        timeout = int(params.get("timeout") or 0) + 15
        last = ""
        for attempt in range(3):
            try:
                with urlopen(req, timeout=timeout) as r:
                    return json.load(r)["result"]
            except urllib.error.HTTPError as e:
                try:
                    info = json.loads(e.read() or b"{}")
                except ValueError:
                    info = {}
                last = info.get("description", str(e))
                if e.code == 429:
                    time.sleep(int(info.get("parameters", {}).get("retry_after", 5)))
                    continue
                if "message is not modified" in last:
                    return {}
                break
            except (OSError, http.client.HTTPException, ValueError) as e:
                last = str(e)
                if method not in RETRIES:
                    break
                time.sleep(5 * (attempt + 1))
        raise ApiError(f"{method}: {last}")

    def send_message(self, chat_id: int, text: str, silent: bool = False, markup: dict | None = None,
                     thread_id: int | None = None) -> int:
        """Send `text` as HTML, into the forum thread `thread_id` when it is given; return the message id."""
        r = self.call("sendMessage", {"chat_id": chat_id, "message_thread_id": thread_id, "text": text,
                                      "parse_mode": "HTML", "disable_notification": silent, "reply_markup": markup})
        return int(r["message_id"])

    def edit_message(self, chat_id: int, msg_id: int, text: str, markup: dict | None = None,
                     entities: list | None = None) -> None:
        """Rewrite a message: HTML, or plain text with `entities` when they are given."""
        params = {"chat_id": chat_id, "message_id": msg_id, "text": text, "reply_markup": markup}
        params.update({"entities": entities} if entities is not None else {"parse_mode": "HTML"})
        self.call("editMessageText", params)

    def send_document(self, chat_id: int, name: str, data: bytes, caption: str = "",
                      thread_id: int | None = None) -> int:
        """Upload `data` as a file called `name` with an HTML caption; return the message id."""
        r = self.call("sendDocument", {"chat_id": chat_id, "message_thread_id": thread_id, "caption": caption or None,
                                       "parse_mode": "HTML"}, files={"document": (name, data)})
        return int(r["message_id"])

    def create_topic(self, chat_id: int, name: str) -> int:
        """Make a forum topic called `name`; return its thread id."""
        return int(self.call("createForumTopic", {"chat_id": chat_id, "name": name})["message_thread_id"])

    def set_reaction(self, chat_id: int, msg_id: int, emoji: str) -> None:
        """Put one emoji reaction on a message, in place of the bot's earlier one."""
        self.call("setMessageReaction", {"chat_id": chat_id, "message_id": msg_id,
                                         "reaction": [{"type": "emoji", "emoji": emoji}]})

    def pin(self, chat_id: int, msg_id: int) -> None:
        """Pin a message without a notification."""
        self.call("pinChatMessage", {"chat_id": chat_id, "message_id": msg_id, "disable_notification": True})

    def unpin(self, chat_id: int, msg_id: int) -> None:
        """Unpin one message."""
        self.call("unpinChatMessage", {"chat_id": chat_id, "message_id": msg_id})

    def answer_callback(self, query_id: str, text: str) -> None:
        """Answer a button press with a short note."""
        self.call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200]})

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """Publish the `/` menu."""
        self.call("setMyCommands", {"commands": commands})

    def get_updates(self, offset: int | None, timeout: int = 60) -> list[dict]:
        """Long-poll for messages and button presses after `offset`."""
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message", "callback_query"]}) or []

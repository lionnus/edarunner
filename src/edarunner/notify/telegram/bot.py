"""The Telegram bot: alerts with buttons, a pinned board, and the command router.

Long polling over outbound HTTPS only; one chat id is obeyed, and one user id when
`user_id` is set.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from edarunner.model import Project, Site
from edarunner.notify import Button, Notifier
from edarunner.notify.telegram import format as fmt
from edarunner.notify.telegram.api import ApiError, BotApi
from edarunner.notify.telegram.commands import HANDLE, Commands, Reply

if TYPE_CHECKING:
    from edarunner.cli import Actions

log = logging.getLogger(__name__)


class TelegramBot(Notifier):
    """One bot, one chat, one poll thread."""

    def __init__(self, site: Site, project: Project, ledger: Any, actions: Actions, token_file: str) -> None:
        assert site.telegram is not None
        self.site = site
        self.tg = site.telegram
        self.project = project
        self.ledger = ledger
        self.actions = actions
        self.api = BotApi(Path(token_file).read_text().strip())
        self.commands = Commands(actions, ledger, self.tg, lambda: self.project, self.repin)
        self.chat_id = int(self.tg.chat_id)
        self.user_id = self.tg.user_id or None
        self._state: dict = ledger.get_kv("telegram", {})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rejected: set[int] = set()

    def _call(self, fn: Any, *args: Any, **kw: Any) -> Any:
        """`fn(*args, **kw)` with an ApiError logged instead of raised."""
        try:
            return fn(*args, **kw)
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None

    # Notifier

    def send(self, kind: str, run_id: str, text: str, buttons: list[Button] | None = None,
             cmd: str | None = None) -> str | None:
        """Send one alert; a repeat with the same kind and run id edits it in place."""
        # A press reaches the watcher that polls, which acts on its own project only.
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]} if (
            buttons and self.project.telegram_poll) else None
        try:
            return str(self._upsert(f"alert:{kind}:{run_id}", fmt.alert(self.project.project, text, cmd), markup))
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None

    def edit(self, msg_id: str, text: str) -> None:
        """Rewrite one message; this drops its buttons."""
        self._call(self.api.edit_message, self.chat_id, int(msg_id), fmt.alert(self.project.project, text))

    def board(self, text: str) -> None:
        """Rewrite the pinned board silently; create and pin it once. `text` is Telegram HTML."""
        title = fmt.head(self.project.project, "board " + time.strftime("%H:%M"))
        try:
            self._upsert("board", fmt.fit(title + "\n" + text), None, silent=True, pin=True)
        except ApiError as e:
            log.warning("telegram: %s", e)

    def repin(self) -> None:
        """Unpin the board message and pin a new one at the bottom of the chat."""
        with self._lock:
            old = self._state.pop("board", None)
        if old is not None:
            self._call(self.api.unpin, self.chat_id, old)
        self.board(self.actions.status_text())

    def _upsert(self, key: str, text: str, markup: dict | None, silent: bool = False, pin: bool = False) -> int:
        """Edit the message kept under `key`, or send a new one and keep its id."""
        with self._lock:
            old = self._state.get(key)
        if old is not None:
            try:
                self.api.edit_message(self.chat_id, old, text, markup)
                return old
            except ApiError as e:
                if "not found" not in str(e):
                    raise
        mid = self.api.send_message(self.chat_id, text, silent=silent, markup=markup)
        if pin:
            self.api.pin(self.chat_id, mid)
        with self._lock:
            self._state[key] = mid
            self.ledger.set_kv("telegram", self._state)
        return mid

    # The poll thread

    def start(self) -> None:
        """Publish the command menu and start the long-poll thread."""
        if not self.project.telegram_poll:
            log.info("telegram: alerts only; another project polls this bot")
            return
        self._call(self.api.set_my_commands, self.commands.menu())
        if not self.user_id:
            log.warning("telegram: no user_id set; chat %d is the only gate, so it must be a private chat", self.chat_id)
        self._thread = threading.Thread(target=self._poll, name="telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the poll thread to end after its current long poll."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _poll(self) -> None:
        offset = None
        while not self._stop.is_set():
            try:
                updates = self.api.get_updates(offset)
            except Exception as e:  # nothing restarts this thread
                log.warning("telegram: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    self.handle_update(u)
                except Exception:  # one bad update must not end the thread
                    log.exception("telegram: update %s", u.get("update_id"))

    # The router

    def handle_update(self, u: dict) -> None:
        """Dispatch one update: a command, a button press, or a stranger."""
        msg, q = u.get("message"), u.get("callback_query")
        m = msg or (q or {}).get("message") or {}
        who = m.get("chat", {}).get("id")
        actor = (q or msg or {}).get("from", {}).get("id")
        if who is None:
            return
        if who != self.chat_id or self.chat_id == 0:
            if who not in self._rejected:
                self._rejected.add(who)
                self.commands.event("rejected", f"chat {who} ignored")
                if self.chat_id == 0:
                    print(f"telegram: the first message came from chat {who}; set chat_id = {who} in [telegram]",
                          file=sys.stderr)
            return
        if self.user_id and actor != self.user_id:
            if actor not in self._rejected:
                self._rejected.add(actor)
                self.commands.event("rejected", f"user {actor} in chat {who} ignored")
            return
        if q:
            self._callback(q)
        elif msg and msg.get("text", "").startswith("/"):
            parts = msg["text"].split()
            self._reply(self.commands.run(parts[0][1:].split("@")[0], parts[1:], msg["text"]))

    def _reply(self, r: Reply) -> None:
        """Send a reply under the bold first line."""
        body = fmt.pre(r.body) if r.kind == "pre" else r.body if r.kind == "html" else fmt.esc(r.body)
        full = fmt.head(self.project.project, r.title) + "\n" + body
        self._call(self.api.send_message, self.chat_id, full if r.kind == "pre" else fmt.fit(full))

    def _callback(self, q: dict) -> None:
        """Act on a button press, answer it, and append the result to the message."""
        verb, _, handle = q.get("data", "").partition(":")
        try:
            if verb == "keep12" and HANDLE.match(handle):
                note = self.actions.keep(handle, 12, "telegram") or f"kept {handle} for 12 h"
            elif verb == "ack" and HANDLE.match(handle):
                note = self.actions.ack(handle, "telegram") or f"acked {handle}"
            else:
                note = "unknown button"
        except Exception as e:
            note = f"error: {e}"
        if note == "unknown button" or note.startswith("error:"):
            self.commands.event("refused", f"button {q.get('data')}: {note}")
        self._call(self.api.answer_callback, q["id"], str(note))
        m = q.get("message")
        if m:
            # Plain text plus the old entities keeps the bold title; the note goes after the last entity.
            self._call(self.api.edit_message, self.chat_id, m["message_id"], m.get("text", "") + "\n" + str(note),
                       m.get("reply_markup"), m.get("entities") or [])

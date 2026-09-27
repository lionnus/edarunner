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
from edarunner.notify.telegram.buttons import Buttons, markup
from edarunner.notify.telegram.commands import Commands, Reply, keyboard_word

if TYPE_CHECKING:
    from edarunner.cli import Actions

log = logging.getLogger(__name__)

REPLY_DAYS = 7  # how long a reply to an alert still finds its run
MAX_DOCUMENT = 20 * 2**20  # the upload limit of the Bot API is 50 MB; a phone needs far less
# The reaction on a command message. ⏳, ✅ and ❌ are not in the set of reactions that Telegram accepts.
REACTIONS = {"busy": "👀", "ok": "👍", "failed": "👎"}


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
        self.buttons = Buttons(actions, self.commands.event)
        self.chat_id = int(self.tg.chat_id)
        self.user_id = self.tg.user_id or None
        self.topic = self.tg.topic_id
        self._state: dict = ledger.get_kv("telegram", {})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rejected: set[int] = set()
        self._topics: set[int] = set()

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
        keys = markup(buttons) if buttons and self.project.telegram_poll else None
        try:
            mid = self._upsert(f"alert:{kind}:{run_id}", fmt.alert(self.project.project, text, cmd), keys)
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None
        if buttons:
            self._remember(mid, run_id)
        return str(mid)

    def _remember(self, msg_id: int, run_id: str) -> None:
        """Keep the run of an alert for REPLY_DAYS, so a reply to it needs no handle."""
        now = time.time()
        with self._lock:
            runs = {k: v for k, v in self._state.get("replies", {}).items() if now - v[1] < REPLY_DAYS * 86400}
            runs[str(msg_id)] = [run_id, now]
            self._state["replies"] = runs
            self.ledger.set_kv("telegram", self._state)

    def _replied_run(self, msg: dict) -> str | None:
        """The run id of the alert that `msg` replies to, or None."""
        target = (msg.get("reply_to_message") or {}).get("message_id")
        with self._lock:
            hit = self._state.get("replies", {}).get(str(target))
        return hit[0] if hit and time.time() - hit[1] < REPLY_DAYS * 86400 else None

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

    def post(self, title: str, html: str, silent: bool = False) -> bool:
        """Send one message: the bold first line with `title`, then `html`."""
        text = fmt.fit(fmt.head(self.project.project, title) + "\n" + html)
        return self._call(self.api.send_message, self.chat_id, text, silent=silent, thread_id=self.topic) is not None

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
        mid = self.api.send_message(self.chat_id, text, silent=silent, markup=markup, thread_id=self.topic)
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
        thread = m.get("message_thread_id") if m.get("is_topic_message") else None
        if not self._in_topic(thread):
            return
        if q:
            self._press(q)
        elif msg and msg.get("text", "").startswith("/"):
            parts = msg["text"].split()
            self._command(msg, parts[0][1:].split("@")[0], parts[1:], self._replied_run(msg), thread)
        elif msg and (word := keyboard_word(msg.get("text", ""))):
            self._command(msg, word, [], None, thread)

    def _command(self, msg: dict, name: str, args: list[str], run: str | None, thread: int | None) -> None:
        """Run one command and react on its message: busy while a slow one runs, then ok or failed."""
        if self.commands.slow(name):
            self._react(msg, "busy")
        r = self.commands.run(name, args, msg["text"] if msg["text"].startswith("/") else "/" + name, run)
        sent = self._reply(r, thread)
        self._react(msg, "ok" if r.ok and sent else "failed")

    def _react(self, msg: dict, what: str) -> None:
        """Set the reaction `what` on a message; a chat without reactions is no fault."""
        try:
            self.api.set_reaction(self.chat_id, msg["message_id"], REACTIONS[what])
        except ApiError as e:
            log.debug("telegram: %s", e)

    def _in_topic(self, thread: int | None) -> bool:
        """True when the bot obeys a message of forum thread `thread`; the first one of a thread prints its id."""
        if self.topic is not None:
            # Another project's watcher answers in its own thread; this one stays silent.
            return thread == self.topic
        if thread is not None and thread not in self._topics:
            self._topics.add(thread)
            print(f"telegram: a message came from topic {thread} of chat {self.chat_id}; "
                  f"set topic_id = {thread} in [telegram] of edr.toml", file=sys.stderr)
        return True

    def _reply(self, r: Reply, thread: int | None = None) -> bool:
        """Send a reply under the bold first line, into the project's topic or the thread of the command.

        True when every part went out; a file over MAX_DOCUMENT goes out as a line that says so.
        """
        thread, ok = self.topic or thread, True
        for d in r.documents:
            if len(d.data) > MAX_DOCUMENT:
                ok = False
                self._reply(Reply(r.title, f"{d.name}: {len(d.data) / 2**20:.1f} MB is over the limit of "
                                           f"{MAX_DOCUMENT // 2**20} MB"), thread)
                continue
            caption = fmt.head(self.project.project, r.title) + (f"\n{fmt.esc(r.body)}" if r.body else "")
            ok &= self._call(self.api.send_document, self.chat_id, d.name, d.data, caption, thread_id=thread) is not None
        if r.documents:
            return ok
        body = fmt.pre(r.body) if r.kind == "pre" else r.body if r.kind == "html" else fmt.esc(r.body)
        full = fmt.head(self.project.project, r.title) + "\n" + body
        return self._call(self.api.send_message, self.chat_id, full if r.kind == "pre" else fmt.fit(full),
                          markup=r.markup, thread_id=thread) is not None

    def _press(self, q: dict) -> None:
        """Answer a button press and rewrite its alert."""
        p = self.buttons.press(q)
        self._call(self.api.answer_callback, q["id"], p.answer)
        m = q.get("message")
        if m:
            # Plain text plus the old entities keeps the bold title; a note goes after the last entity.
            self._call(self.api.edit_message, self.chat_id, m["message_id"], p.text, p.markup, m.get("entities") or [])

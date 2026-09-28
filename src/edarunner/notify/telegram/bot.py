"""The Telegram bot: alerts with buttons, a pinned board, and the commands of every project.

Every watcher sends its alerts, and keeps the run of each alert with buttons in the database of its
project. Only the holder of `~/.edr/serve.lock`, the supervisor or else the first watcher, polls:
Telegram takes one poller per token. Its router finds the project of every command and button
press. Long polling runs over outbound HTTPS only; one chat id is obeyed, and one user id when
`user_id` is set.

With `topics = true` in `user.toml`, each watcher sends into the forum topic of its project and
makes the topic the first time; the topic id lives in the database of the project. The
supervisor sends the global board and the digest to the main thread. A command in the topic of a
project acts on that project.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from edarunner.model import Project, Site, Telegram
from edarunner.notify import Notifier
from edarunner.notify.telegram import format as fmt
from edarunner.notify.telegram.api import ApiError, BotApi
from edarunner.notify.telegram.buttons import Buttons, markup
from edarunner.notify.telegram.commands import Commands, Reply, keyboard_word

if TYPE_CHECKING:
    from edarunner.cli import Router
    from edarunner.notify.alerts import Alert

log = logging.getLogger(__name__)

REPLY_DAYS = 7  # how long a reply to an alert and a button press still find its run
MAX_DOCUMENT = 20 * 2**20  # the upload limit of the Bot API is 50 MB; a phone needs far less
# The reaction on a command message. ⏳, ✅ and ❌ are not in the set of reactions that Telegram accepts.
REACTIONS = {"busy": "👀", "ok": "👍", "failed": "👎"}


class TelegramBot(Notifier):
    """One bot and one chat; with a router it also takes the commands and button presses of every project.

    The custom commands are those of `site` and those of `tg`; with `topic`, the alerts, the board and
    every post go into the forum topic of `project` when `tg.topics` is on."""

    def __init__(self, tg: Telegram, site: Site | None, project: Project, db: Any, router: Router | None,
                 token_file: str, topic: bool = True) -> None:
        self.tg = tg
        self.project = project
        self.db = db
        self.router = router
        self.api = BotApi(Path(token_file).read_text().strip())
        custom = {**(site.commands if site else {}), **tg.commands}
        self.commands = Commands(router, db, custom, str(site.path.parent) if site else "", self.repin) if router else None
        self.buttons = Buttons(router, db, self.commands.event) if router and self.commands else None
        self.chat_id = int(tg.chat_id)
        self.user_id = tg.user_id or None
        self.topic = topic and tg.topics
        self._state: dict = db.get_store("telegram", {})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rejected: set[int] = set()
        self._no_topic = False

    def _call(self, fn: Any, *args: Any, **kw: Any) -> Any:
        """`fn(*args, **kw)` with an ApiError logged instead of raised."""
        try:
            return fn(*args, **kw)
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None

    # Notifier

    def send(self, alert: Alert) -> str | None:
        """Send one alert; a repeat with the same kind and key edits it in place."""
        keys = markup(alert.buttons) if alert.buttons else None
        try:
            mid = self._upsert(f"alert:{alert.kind}:{alert.key}", fmt.alert(self.project.project, alert), keys)
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None
        if alert.buttons:
            self._remember(mid, alert.key)
        return str(mid)

    def _remember(self, msg_id: int, run_id: str) -> None:
        """Keep the run of an alert for REPLY_DAYS, so a press or a reply finds it."""
        now = time.time()
        with self._lock:
            runs = {k: v for k, v in self._state.get("replies", {}).items() if now - v[1] < REPLY_DAYS * 86400}
            runs[str(msg_id)] = [run_id, now]
            self._state["replies"] = runs
            self.db.set_store("telegram", self._state)

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
        return self._call(self._new, text, silent) is not None

    def _topic(self) -> int | None:
        """The forum topic of the project, made the first time; None for the main thread."""
        if not self.topic or self._no_topic:
            return None
        with self._lock:
            tid = self._state.get("topic")
        if tid is None:
            try:
                tid = self.api.create_topic(self.chat_id, self.project.project)
            except ApiError as e:
                self._no_topic = True
                log.warning("telegram: no topic for %s, so the main thread: %s", self.project.project, e)
                return None
            with self._lock:
                self._state["topic"] = tid
                self.db.set_store("telegram", self._state)
        return tid

    def _new(self, text: str, silent: bool = False, markup: dict | None = None) -> int:
        """Send a new message into the topic of the project; a topic that someone deleted is made again."""
        tid = self._topic()
        try:
            return self.api.send_message(self.chat_id, text, silent=silent, markup=markup, thread_id=tid)
        except ApiError as e:
            if tid is None or "thread not found" not in str(e):
                raise
        with self._lock:
            self._state.pop("topic", None)
        return self.api.send_message(self.chat_id, text, silent=silent, markup=markup, thread_id=self._topic())

    def repin(self) -> None:
        """Unpin the board message and pin a new one at the bottom of the chat."""
        with self._lock:
            old = self._state.pop("board", None)
        if old is not None:
            self._call(self.api.unpin, self.chat_id, old)
        if self.router is not None:
            self.board(self.router.board_text())

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
        mid = self._new(text, silent, markup)
        if pin:
            self.api.pin(self.chat_id, mid)
        with self._lock:
            self._state[key] = mid
            self.db.set_store("telegram", self._state)
        return mid

    # The poll thread

    def start(self) -> None:
        """Publish the command menu and start the long-poll thread; a bot without a router only sends."""
        if self.commands is None:
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
        if self.commands is None or self.buttons is None or self.router is None:
            return
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
        if q:
            self._press(q)
        elif msg and msg.get("text", "").startswith("/"):
            parts = msg["text"].split()
            target = (msg.get("reply_to_message") or {}).get("message_id")
            run = self.router.replied(int(target)) if target else None
            self._command(msg, parts[0][1:].split("@")[0], parts[1:], run, thread)
        elif msg and (word := keyboard_word(msg.get("text", ""))):
            self._command(msg, word, [], None, thread)

    def _command(self, msg: dict, name: str, args: list[str], run: tuple[str, str] | None, thread: int | None) -> None:
        """Run one command and react on its message: busy while a slow one runs, then ok or failed."""
        assert self.commands is not None
        if self.commands.slow(name):
            self._react(msg, "busy")
        assert self.router is not None
        here = self.router.topic_of(thread) if thread else None
        r = self.commands.run(name, args, msg["text"] if msg["text"].startswith("/") else "/" + name, run, here)
        sent = self._reply(r, thread)
        self._react(msg, "ok" if r.ok and sent else "failed")

    def _react(self, msg: dict, what: str) -> None:
        """Set the reaction `what` on a message; a chat without reactions is no fault."""
        try:
            self.api.set_reaction(self.chat_id, msg["message_id"], REACTIONS[what])
        except ApiError as e:
            log.debug("telegram: %s", e)

    def _reply(self, r: Reply, thread: int | None = None) -> bool:
        """Send a reply under the bold first line, into the thread of the command.

        True when every part went out; a file over MAX_DOCUMENT goes out as a line that says so.
        """
        ok, name = True, r.project or self.project.project
        for d in r.documents:
            if len(d.data) > MAX_DOCUMENT:
                ok = False
                self._reply(Reply(r.title, f"{d.name}: {len(d.data) / 2**20:.1f} MB is over the limit of "
                                           f"{MAX_DOCUMENT // 2**20} MB", project=r.project), thread)
                continue
            caption = fmt.head(name, r.title) + (f"\n{fmt.esc(r.body)}" if r.body else "")
            ok &= self._call(self.api.send_document, self.chat_id, d.name, d.data, caption, thread_id=thread) is not None
        if r.documents:
            return ok
        body = fmt.pre(r.body) if r.kind == "pre" else r.body if r.kind == "html" else fmt.esc(r.body)
        full = fmt.head(name, r.title) + "\n" + body
        return self._call(self.api.send_message, self.chat_id, full if r.kind == "pre" else fmt.fit(full),
                          markup=r.markup, thread_id=thread) is not None

    def _press(self, q: dict) -> None:
        """Answer a button press and rewrite its alert."""
        assert self.buttons is not None
        p = self.buttons.press(q)
        self._call(self.api.answer_callback, q["id"], p.answer)
        m = q.get("message")
        if m:
            # Plain text plus the old entities keeps the bold title; a note goes after the last entity.
            self._call(self.api.edit_message, self.chat_id, m["message_id"], p.text, p.markup, m.get("entities") or [])

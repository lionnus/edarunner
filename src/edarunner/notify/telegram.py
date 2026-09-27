"""The Telegram bot: alerts with buttons, a pinned board, commands.

Long polling over outbound HTTPS only; one chat id is obeyed, and one user id when
`user_id` is set. Every HTTP call goes through `TelegramBot.api`, so a
test replaces that one method.
"""

from __future__ import annotations

import getpass
import html
import http.client
import json
import logging
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

from edarunner.model import BotCommand, Project, Site
from edarunner.notify import Button, Notifier

if TYPE_CHECKING:
    from edarunner.cli import Actions

log = logging.getLogger(__name__)

HANDLE = re.compile(r"^[\w.@#-]{1,64}$")
LIMIT = 4000  # the message limit is 4096 characters after parsing
# Only an idempotent call is sent again after a network failure; a resent sendMessage posts twice.
RETRIES = {"getUpdates", "editMessageText", "answerCallbackQuery"}
BUILTINS = {
    "status": "the narrow board",
    "events": "the last events: /events [n]",
    "hosts": "free cores, RAM and scratch per host",
    "lic": "free licence seats",
    "board": "pin a new board message",
    "keep": "add hours to a run: /keep <handle> [hours]",
    "ack": "cancel a pending kill: /ack <handle>",
    "stop": "stop after the running task: /stop <handle> [why]",
    "compare": "metrics side by side: /compare <handle>...",
    "metric": "one metric per run: /metric <name> [--design H]",
    "help": "this list",
}


class ApiError(Exception):
    """Telegram refused a call, or the network failed after the retries."""


def pre(text: str) -> str:
    """The last 4000 characters of `text` as an HTML <pre> block."""
    return "<pre>" + html.escape(str(text)[-LIMIT:], quote=False) + "</pre>"


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


class TelegramBot(Notifier):
    """One bot, one chat, one poll thread."""

    def __init__(self, site: Site, project: Project, ledger: object, actions: Actions, token_file: str) -> None:
        assert site.telegram is not None
        self.site = site
        self.tg = site.telegram
        self.project = project
        self.ledger = ledger
        self.actions = actions
        self.token = Path(token_file).read_text().strip()
        self.chat_id = int(self.tg.chat_id)
        self.user_id = self.tg.user_id or None
        self._state: dict = ledger.get_kv("telegram", {})  # type: ignore[attr-defined]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rejected: set[int] = set()

    # HTTP

    def api(self, method: str, params: dict, files: dict[str, tuple[str, bytes]] | None = None) -> object:
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

    def _call(self, method: str, params: dict) -> object | None:
        try:
            return self.api(method, params)
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None

    # Notifier

    def send(self, kind: str, run_id: str, text: str, buttons: list[Button] | None = None) -> str | None:
        """Send one alert; a repeat with the same kind and run id edits it in place."""
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]} if buttons else None
        try:
            return str(self._upsert(f"alert:{kind}:{run_id}", pre(text), markup))
        except ApiError as e:
            log.warning("telegram: %s", e)
            return None

    def edit(self, msg_id: str, text: str) -> None:
        """Rewrite one message; this drops its buttons."""
        self._call("editMessageText", self._edit_params(int(msg_id), pre(text), None))

    def board(self, text: str) -> None:
        """Rewrite the pinned board silently; create and pin it once."""
        try:
            self._upsert("board", pre(text), None, silent=True, pin=True)
        except ApiError as e:
            log.warning("telegram: %s", e)

    def _edit_params(self, msg_id: int, text: str, markup: dict | None) -> dict:
        return {"chat_id": self.chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML", "reply_markup": markup}

    def _upsert(self, key: str, text: str, markup: dict | None, silent: bool = False, pin: bool = False) -> int:
        with self._lock:
            old = self._state.get(key)
        if old is not None:
            try:
                self.api("editMessageText", self._edit_params(old, text, markup))
                return old
            except ApiError as e:
                if "not found" not in str(e):
                    raise
        r = self.api("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_notification": silent, "reply_markup": markup})
        mid = int(r["message_id"])  # type: ignore[index]
        if pin:
            self.api("pinChatMessage", {"chat_id": self.chat_id, "message_id": mid, "disable_notification": True})
        with self._lock:
            self._state[key] = mid
            self.ledger.set_kv("telegram", self._state)  # type: ignore[attr-defined]
        return mid

    # The poll thread

    def start(self) -> None:
        """Publish the command menu and start the long-poll thread."""
        menu = [{"command": c, "description": h[:256]} for c, h in BUILTINS.items()]
        menu += [{"command": n, "description": (c.help or n)[:256]} for n, c in self.tg.commands.items()]
        self._call("setMyCommands", {"commands": menu})
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
                updates = self.api("getUpdates", {"offset": offset, "timeout": 60, "allowed_updates": ["message", "callback_query"]})
            except Exception as e:  # nothing restarts this thread
                log.warning("telegram: %s", e)
                time.sleep(5)
                continue
            for u in updates or []:  # type: ignore[union-attr]
                offset = u["update_id"] + 1
                try:
                    self.handle_update(u)
                except Exception:  # one bad update must not end the thread
                    log.exception("telegram: update %s", u.get("update_id"))

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
                self._event("rejected", f"chat {who} ignored")
                if self.chat_id == 0:
                    print(f"telegram: the first message came from chat {who}; set chat_id = {who} in site.toml", file=sys.stderr)
            return
        if self.user_id and actor != self.user_id:
            if actor not in self._rejected:
                self._rejected.add(actor)
                self._event("rejected", f"user {actor} in chat {who} ignored")
            return
        if q:
            self._callback(q)
        elif msg and msg.get("text", "").startswith("/"):
            self._command(msg["text"])

    def _event(self, kind: str, text: str) -> None:
        self.ledger.add_event(actor="telegram", run_id="", kind=kind, text=text)  # type: ignore[attr-defined]

    def _reply(self, text: object) -> None:
        self._call("sendMessage", {"chat_id": self.chat_id, "text": pre(str(text)), "parse_mode": "HTML"})

    # Commands

    def _command(self, text: str) -> None:
        parts = text.split()
        name, args = parts[0][1:].split("@")[0], parts[1:]
        try:
            if name in self.tg.commands:
                out = self._custom(self.tg.commands[name], args)
            else:
                fn = getattr(self, "_cmd_" + name) if name in BUILTINS else self._cmd_help
                out = fn(args)
                self._event("command", text[:200])
        except Exception as e:  # a refused handle is an answer, not a crash
            out = f"error: {e}"
            self._event("refused", f"{text[:200]}: {e}")
        self._reply(out)

    def _handle(self, args: list[str]) -> str:
        if not args or not HANDLE.match(args[0]):
            raise ValueError("a handle is label@batch, a run id prefix or #n")
        return args[0]

    def _cmd_status(self, args: list[str]) -> str:
        return self.actions.status_text(narrow=True)

    def _cmd_events(self, args: list[str]) -> str:
        return self.actions.events_text(int(args[0]) if args and args[0].isdigit() else 10)

    def _cmd_hosts(self, args: list[str]) -> str:
        return self.actions.hosts_text()

    def _cmd_lic(self, args: list[str]) -> str:
        return self.actions.lic_text()

    def _cmd_board(self, args: list[str]) -> str:
        with self._lock:
            old = self._state.pop("board", None)
        if old is not None:
            self._call("unpinChatMessage", {"chat_id": self.chat_id, "message_id": old})
        self.board(self.actions.status_text(narrow=True))
        return "board pinned"

    def _cmd_keep(self, args: list[str]) -> str:
        handle = self._handle(args)
        hours = int(args[1]) if len(args) > 1 and args[1].isdigit() else 12
        return self.actions.keep(handle, hours, "telegram") or f"kept {handle} for {hours} h"

    def _cmd_ack(self, args: list[str]) -> str:
        handle = self._handle(args)
        return self.actions.ack(handle, "telegram") or f"acked {handle}"

    def _cmd_stop(self, args: list[str]) -> str:
        handle = self._handle(args)
        why = " ".join(args[1:]) or "stopped from telegram"
        return self.actions.stop_after_task(handle, "telegram", why) or f"{handle} stops after its task"

    def _cmd_compare(self, args: list[str]) -> str:
        if not args:
            return "usage: /compare <handle>..."
        return self.actions.compare_text([self._handle([a]) for a in args])

    def _cmd_metric(self, args: list[str]) -> str:
        if not args:
            return "usage: /metric <name> [--design H]"
        design = args[args.index("--design") + 1] if "--design" in args[:-1] else None
        return self.actions.metric_text(args[0], design)

    def _cmd_help(self, args: list[str]) -> str:
        lines = [f"/{c} - {h}" for c, h in BUILTINS.items()]
        lines += [f"/{n} - {c.help}" for n, c in self.tg.commands.items()]
        return "\n".join(lines)

    def _callback(self, q: dict) -> None:
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
        self._event("button", f"{q.get('data')}: {note}")
        self._call("answerCallbackQuery", {"callback_query_id": q["id"], "text": str(note)[:200]})
        m = q.get("message")
        if m:
            self._call("editMessageText", self._edit_params(m["message_id"], pre(m.get("text", "") + "\n" + str(note)), m.get("reply_markup")))

    def _custom(self, c: BotCommand, args: list[str]) -> str:
        names = list(c.args)
        if names and len(args) >= len(names):
            values = args[: len(names) - 1] + [" ".join(args[len(names) - 1 :])]  # the last argument takes the rest
        else:
            values = args
        if len(values) != len(names):
            return f"usage: /{c.name} " + " ".join(f"<{n}>" for n in names)
        root = str(self.project.root)
        env = {"project": self.project.project, "root": root, "project_root": root, "site_dir": str(self.site.path.parent), "user": getpass.getuser()}
        for n, v in zip(names, values):
            if not re.fullmatch(c.args[n], v):
                self._event("refused", f"/{c.name} {n}={v!r} does not match {c.args[n]}")
                return f"refused: {n} must match {c.args[n]}"
            env[n] = v
        try:
            argv = [t.format_map(env) for t in c.run]
            cwd = (c.cwd or "{root}").format_map(env)
            skip = [t.format_map(env) for t in c.skip_if or []]
            skip_reply = (c.skip_reply or "skipped").format_map(env)
            reply = c.reply.format_map(env)
        except (KeyError, IndexError, ValueError) as e:
            return f"/{c.name}: bad placeholder {e}"
        self._event("command", f"/{c.name} " + " ".join(f"{n}={v}" for n, v in zip(names, values)))
        if c.dry_run:
            return f"would run in {cwd}:\n{shlex.join(argv)}"
        try:
            if skip and subprocess.run(skip, capture_output=True, cwd=cwd, timeout=c.timeout_s).returncode == 0:
                return skip_reply
            if c.detach:
                logf = Path(self.project.data) / f"telegram-{c.name}.log"
                logf.parent.mkdir(parents=True, exist_ok=True)
                with open(logf, "ab") as f:
                    p = subprocess.Popen(argv, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
                return f"{reply or 'started'} (pid {p.pid}, log {logf})"
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace", cwd=cwd, timeout=c.timeout_s)
        except subprocess.TimeoutExpired:
            return f"/{c.name}: timed out after {c.timeout_s} s"
        except OSError as e:
            return f"/{c.name}: {e}"
        if r.returncode == 0 and reply:
            return reply
        out = (r.stdout + r.stderr).strip()
        return out or f"(no output, exit {r.returncode})"

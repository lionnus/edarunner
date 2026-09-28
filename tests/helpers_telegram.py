"""A recording Telegram API, fake actions and database, and the site and project of the bot tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

from edarunner.model import BotCommand, Host, Site, Telegram
from edarunner.notify.telegram.api import BotApi
from helpers_driver import DEMO

CHAT = 42
USER = 12345


class FakeApi(BotApi):
    """Records every call; a send returns the next message id."""

    def __init__(self) -> None:
        super().__init__("123:ABC")
        self.calls: list[tuple[str, dict]] = []
        self.n = 0

    def call(self, method: str, params: dict, files=None) -> dict:
        self.calls.append((method, {**params, "files": files} if files else params))
        if method in ("sendMessage", "sendDocument"):
            self.n += 1
            return {"message_id": self.n}
        return {}

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.board: list[Path] = []

    def __getattr__(self, name: str):
        def f(*a, **k):
            self.calls.append((name, a, k))
            return f"{name} ok"

        return f

    def log_tail(self, handle: str, n: int) -> tuple[str, bytes]:
        self.calls.append(("log_tail", (handle, n), {}))
        return f"{handle}.log", b"line\n" * n

    def board_files(self) -> list[Path]:
        return self.board

    def metrics_csv(self, source: str) -> bytes:
        return f"run_id,source\nr1,{source}\n".encode()

    def run_info(self, handle: str) -> dict:
        self.calls.append(("run_info", (handle,), {}))
        return {"handle": "a@demo", "run_id": handle, "run_root": "/scratch/edr/demo/" + handle, "host": "hostA"}


class FakeDatabase:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.store: dict = {}

    def add_event(self, **kw) -> None:
        self.events.append(kw)

    def get_store(self, key, default=None):
        return self.store.get(key, default)

    def set_store(self, key, value) -> None:
        self.store[key] = json.loads(json.dumps(value))


COMMANDS = {
    "echo": BotCommand("echo", "echo a dir", ["echo", "{project}/{dir}"], args={"dir": "^(backend|paper)$"}),
    "dry": BotCommand("dry", "dry", ["rm", "-rf", "{dir}"], args={"dir": "^\\w+$"}, dry_run=True),
    "skip": BotCommand("skip", "skip", ["false"], skip_if=["true"], skip_reply="already {project}"),
    "bg": BotCommand("bg", "bg", ["sleep", "1"], detach=True, reply="started {project}"),
    "dies": BotCommand("dies", "dies", ["sh", "-c", "echo loading; echo Workspace not trusted; exit 3"], detach=True),
    "ask": BotCommand("ask", "ask", ["echo", "{question}"], args={"question": "^[\\w ?]{1,40}$"}),
    "slow": BotCommand("slow", "slow", ["sleep", "5"], timeout_s=1),
    "where": BotCommand("where", "where", ["echo", "{handle} {run_id} {host}:{run_root}"]),
    "same": BotCommand("same", "same", ["echo", "ran {dir}"], args={"dir": "^\\w+$"},
                       skip_if=["test", "{dir}", "=", "{project}"], skip_reply="skipped {dir}"),
}


def make_site(tmp_path: Path, chat_id: int = CHAT, mode: int = 0o600, user_id: int | None = None) -> Site:
    token = tmp_path / "telegram.token"
    token.write_text("123:ABC\n")
    token.chmod(mode)
    return Site(path=DEMO / "site.toml", scratch=["/tmp/edr-demo"], env={}, ssh_options=[], ssh_timeout_s=20,
                tool_procs="^(sleep)$", hosts={"local": Host("local", 4, 8)},
                telegram=Telegram(token_file=token, chat_id=chat_id, commands=dict(COMMANDS), user_id=user_id))


def make_project(tmp_path: Path) -> SimpleNamespace:
    # A Project needs every stage; the bot reads a few fields of it.
    edr = tomllib.loads((DEMO / "edr.toml").read_text())
    return SimpleNamespace(project=edr["project"], root=DEMO, data=tmp_path / "data", telegram_poll=True,
                           site=SimpleNamespace(path=DEMO / "site.toml"))

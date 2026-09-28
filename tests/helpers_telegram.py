"""A recording Telegram API, a fake router over two projects, a fake database, and the site and project of the bot tests."""

from __future__ import annotations

import contextlib
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

from edarunner.guards import Refuse
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
    def __init__(self, project: SimpleNamespace | None = None, calls: list | None = None) -> None:
        self.calls: list[tuple] = [] if calls is None else calls
        self.board: list[Path] = []
        self.c = SimpleNamespace(project=project)

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


class FakeRouter:
    """The projects `names`, demo first, whose actions share one call list. A handle resolves in demo, `both@x` in
    every project, `project/handle` in its project; a message id maps to a run through `replies`."""

    def __init__(self, project: SimpleNamespace, names: tuple[str, ...] = ("demo",)) -> None:
        self.calls: list[tuple] = []
        self.acts = {n: FakeActions(project, self.calls) for n in names}
        self.replies: dict[tuple[str, int], str] = {}

    def names(self) -> list[str]:
        return list(self.acts)

    @contextlib.contextmanager
    def actions(self, name: str):
        if name not in self.acts:
            raise Refuse(f"no registered project {name}")
        yield self.acts[name]

    def pick(self, args: list[str], run):
        if args and args[0] in self.acts:
            return args[0], args[1:]
        if run or len(self.acts) == 1:
            return (run or ("demo",))[0], args
        raise Refuse("name a project first: " + ", ".join(self.acts))

    def resolve(self, handle: str) -> tuple[str, str]:
        if "/" in handle:
            project, _, h = handle.partition("/")
            return project, h
        if handle.startswith("both") and len(self.acts) > 1:
            raise Refuse(f"{handle}: more than one project has it: " + ", ".join(f"{n}/{handle}" for n in self.acts))
        return "demo", handle

    def run_of(self, project: str, msg_id: int) -> str | None:
        return self.replies.get((project, msg_id))

    def replied(self, msg_id: int):
        return next(((p, r) for (p, m), r in self.replies.items() if m == msg_id), None)

    def __getattr__(self, name: str):
        def f(*a, **k):
            self.calls.append((name, a, k))
            return f"{name} ok"

        return f


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
    return SimpleNamespace(project=edr["project"], root=DEMO, data=tmp_path / "data", site=SimpleNamespace(path=DEMO / "site.toml"))

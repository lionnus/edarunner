"""The commands of the bot: the built-in table, one handler per built-in, and the custom argv commands.

A handler turns the arguments of one message into a `Reply`; the bot sends it.
"""

from __future__ import annotations

import getpass
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from edarunner.model import BotCommand, Project, Telegram
from edarunner.notify.telegram import format as fmt
from edarunner.notify.telegram.custom import run_custom

if TYPE_CHECKING:
    from edarunner.cli import Actions

HANDLE = re.compile(r"^[\w.@#-]{1,128}$")
DESIGN = re.compile(r"^[\w.-]{1,64}$")
LOG_LINES = 200
# The words of the reply keyboard, in rows; a tap sends the word, which runs the command of that name.
KEYBOARD = (("Status", "Hosts"), ("Events", "Tools", "Digest"))
ALIASES = {"lic": "tools"}  # old names, gone in the next release


@dataclass(frozen=True)
class Builtin:
    """One built-in command: its menu entry, its group in /help, and how its reply is sent."""

    name: str
    args: str
    help: str
    group: str
    kind: str = "text"  # "html" as it is, "pre" in a <pre> block, "text" escaped
    self_logged: bool = False  # the action writes its own ledger event
    on_run: bool = False  # the first argument is a handle; a reply to an alert fills it
    slow: bool = False  # it can take more than a second, so its message gets a reaction first


BUILTINS = {b.name: b for b in (
    Builtin("status", "[handle]", "the board, or one run", "Look", "html", on_run=True),
    Builtin("events", "[n]", "the last events, newest first", "Look", "html"),
    Builtin("hosts", "", "cores, RAM, scratch and GPUs, used of total", "Look", "html", slow=True),
    Builtin("tools", "", "seats used of total, and the hosts, per tool", "Look", "html"),
    Builtin("digest", "", "the daily digest now", "Look", "html"),
    Builtin("pin", "", "pin a new board message", "Look"),
    Builtin("log", "<handle> [n]", "the last n log lines as a file, default 200", "Files", on_run=True, slow=True),
    Builtin("board", "", "compare.html and status.html as files", "Files"),
    Builtin("csv", "<design>", "the metrics of one design as a CSV file", "Files"),
    Builtin("keep", "<handle> [hours]", "add hours, default 12", "Act on a run", self_logged=True, on_run=True),
    Builtin("ack", "<handle>", "cancel a pending kill", "Act on a run", self_logged=True, on_run=True),
    Builtin("stop", "<handle> [why]", "stop after the running task", "Act on a run", self_logged=True, on_run=True),
    Builtin("compare", "<handle>...", "metrics side by side", "Compare", "pre"),
    Builtin("metric", "<name> [--design H]", "one metric per run", "Compare", "pre"),
    Builtin("help", "", "this list", "Help", "html"),
    Builtin("start", "", "this list and the reply keyboard", "Help", "html"),
    Builtin("keyboard", "[off]", "show or remove the reply keyboard", "Help"),
)}


@dataclass
class Document:
    """One file to upload: its name on the phone and its bytes."""

    name: str
    data: bytes


@dataclass
class Reply:
    """One answer to a command: the title of its first line, the body, how the body is sent, and files."""

    title: str
    body: str
    kind: str = "text"
    ok: bool = True
    documents: list[Document] = field(default_factory=list)
    markup: dict | None = None


def handle(args: list[str]) -> str:
    """The first argument as a run handle; ValueError when it is not one."""
    if not args or not HANDLE.match(args[0]):
        raise ValueError("a handle is label@batch, a run id prefix or #n")
    return args[0]


def keyboard() -> dict:
    """The persistent reply keyboard of KEYBOARD."""
    return {"keyboard": [[{"text": w} for w in row] for row in KEYBOARD], "resize_keyboard": True, "is_persistent": True}


def keyboard_word(text: str) -> str | None:
    """The command of a keyboard word, such as `status` for `Status`; None for other text."""
    word = text.strip().lower()
    return word if any(word == w.lower() for row in KEYBOARD for w in row) else None


class Commands:
    """The built-in and custom commands of one bot, on the verbs of `Actions`."""

    def __init__(self, actions: Actions, ledger: Any, tg: Telegram, project: Callable[[], Project],
                 repin: Callable[[], None]) -> None:
        self.actions = actions
        self.ledger = ledger
        self.tg = tg
        self.project = project
        self.repin = repin

    def menu(self) -> list[dict[str, str]]:
        """The `/` menu: every built-in, then every custom command."""
        out = [{"command": b.name, "description": (f"{b.help}: /{b.name} {b.args}" if b.args else b.help)[:256]}
               for b in BUILTINS.values()]
        return out + [{"command": n, "description": (c.help or n)[:256]} for n, c in self.tg.commands.items()]

    def slow(self, name: str) -> bool:
        """True for a command that can take more than a second: a custom one, or a slow built-in."""
        return name in self.tg.commands or (name in BUILTINS and BUILTINS[name].slow)

    def run(self, name: str, args: list[str], text: str, run: str | None = None) -> Reply:
        """Answer one command; an unknown name gets the help. Every command lands in the ledger.

        `run` is the run of the alert the message replies to: it fills the handle of a built-in
        and the run placeholders of a custom command.
        """
        name = ALIASES.get(name, name)
        if name not in self.tg.commands and name not in BUILTINS:
            name = "help"
        try:
            if name in self.tg.commands:
                return self.custom(self.tg.commands[name], args, self.actions.run_info(run) if run else {})
            b = BUILTINS[name]
            if run and b.on_run and args[:1] != [run]:
                args = [run, *args]
            out = getattr(self, "cmd_" + name)(args)
            if not b.self_logged:
                self.event("command", text[:200])
            return out if isinstance(out, Reply) else Reply(name, out, b.kind)
        except Exception as e:  # a refused handle is an answer, not a crash
            self.event("refused", f"{text[:200]}: {e}")
            return Reply(name, f"error: {e}", ok=False)

    def custom(self, c: BotCommand, args: list[str], extra: dict[str, str]) -> Reply:
        """Run a custom command with the project placeholders plus `extra`, such as the run of an alert."""
        project = self.project()
        root = str(project.root)
        env = {"project": project.project, "root": root, "project_root": root,
               "site_dir": str(project.site.path.parent), "user": getpass.getuser(), **extra}
        text, ok = run_custom(c, args, env, Path(project.data), self.event)
        return Reply(c.name, text, "pre", ok)

    def event(self, kind: str, text: str) -> None:
        """One ledger event with the actor telegram."""
        self.ledger.add_event(actor="telegram", run_id="", kind=kind, text=text)

    # built-in commands

    def cmd_status(self, args: list[str]) -> str:
        """The board, or one run."""
        return self.actions.status_text(handle(args) if args else None)

    def cmd_events(self, args: list[str]) -> str:
        """The last n events, default 8, at most 30."""
        return self.actions.events_text(min(30, int(args[0])) if args and args[0].isdecimal() else 8)

    def cmd_hosts(self, args: list[str]) -> str:
        """One line per host."""
        return self.actions.hosts_text()

    def cmd_tools(self, args: list[str]) -> str:
        """One line per tool."""
        return self.actions.tools_text()

    def cmd_pin(self, args: list[str]) -> str:
        """Unpin the board and pin a new one."""
        self.repin()
        return "board pinned"

    def cmd_log(self, args: list[str]) -> Reply:
        """The last n lines, default 200, of the log of a run as a file `<handle>.log`."""
        h = handle(args)
        n = int(args[1]) if len(args) > 1 and args[1].isdecimal() else LOG_LINES
        name, data = self.actions.log_tail(h, n)
        return Reply("log", f"the last {n} lines", documents=[Document(name, data)])

    def cmd_board(self, args: list[str]) -> Reply | str:
        """compare.html and status.html of the last watcher cycle as files."""
        files = self.actions.board_files()
        if not files:
            return "no board files yet; the watcher writes them every cycle"
        return Reply("board", "", documents=[Document(p.name, p.read_bytes()) for p in files])

    def cmd_csv(self, args: list[str]) -> Reply | str:
        """The metrics of one design as `metrics.csv`."""
        if not args or not DESIGN.match(args[0]):
            return "usage: /csv <design>"
        return Reply("csv", args[0], documents=[Document("metrics.csv", self.actions.metrics_csv(args[0]))])

    def cmd_digest(self, args: list[str]) -> str:
        """The daily digest now."""
        return self.actions.digest_text()

    def cmd_keep(self, args: list[str]) -> str:
        """Add hours, default 12, to the running stage or task."""
        h = handle(args)
        hours = int(args[1]) if len(args) > 1 and args[1].isdigit() else 12
        return self.actions.keep(h, hours, "telegram") or f"kept {h} for {hours} h"

    def cmd_ack(self, args: list[str]) -> str:
        """Cancel the pending kill of a run."""
        h = handle(args)
        return self.actions.ack(h, "telegram") or f"acked {h}"

    def cmd_stop(self, args: list[str]) -> str:
        """Stop a run after its running task."""
        h = handle(args)
        why = " ".join(args[1:]) or "stopped from telegram"
        return self.actions.stop_after_task(h, "telegram", why) or f"{h} stops after its task"

    def cmd_compare(self, args: list[str]) -> str:
        """The metrics of several runs side by side."""
        if not args:
            return "usage: /compare <handle>..."
        return self.actions.compare_text([handle([a]) for a in args])

    def cmd_metric(self, args: list[str]) -> str:
        """One metric for every run, or for the runs of one design."""
        if not args:
            return "usage: /metric <name> [--design H]"
        design = args[args.index("--design") + 1] if "--design" in args[:-1] else None
        return self.actions.metric_text(args[0], design)

    def cmd_help(self, args: list[str]) -> str:
        """The built-in commands by group, then the custom commands."""
        groups: dict[str, list[tuple[str, str]]] = {}
        for b in BUILTINS.values():
            groups.setdefault(b.group, []).append((f"/{b.name} {b.args}", b.help))
        if self.tg.commands:
            groups["Custom"] = [(f"/{n} " + " ".join(f"<{a}>" for a in c.args), c.help or "")
                                for n, c in self.tg.commands.items()]
        return fmt.help_text(groups)

    def cmd_start(self, args: list[str]) -> Reply:
        """The help, with the reply keyboard."""
        return Reply("help", self.cmd_help(args), "html", markup=keyboard())

    def cmd_keyboard(self, args: list[str]) -> Reply:
        """Show the reply keyboard, or remove it with `off`."""
        if args[:1] == ["off"]:
            return Reply("keyboard", "keyboard off", markup={"remove_keyboard": True})
        return Reply("keyboard", "keyboard on", markup=keyboard())

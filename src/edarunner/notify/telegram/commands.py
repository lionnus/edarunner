"""The commands of the bot: the built-in table, one handler per built-in, and the custom argv commands.

A handler turns the arguments of one message into a `Reply`; the bot sends it. The router finds
the project of a command first and the run second. A handle `project/label@batch` names both, and
`#n` is run n of the last global board. A bare handle or a run id prefix is searched in every
project: one match acts, and several get the list of `project/label@batch` to copy and no action.
A command that needs a project and names none takes the project of the alert it replies to, else
the project of the forum topic it came from, else the only registered project; else the reply
lists the names.
"""

from __future__ import annotations

import getpass
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from edarunner import home
from edarunner.model import BotCommand
from edarunner.notify.telegram import format as fmt
from edarunner.notify.telegram.custom import run_custom

if TYPE_CHECKING:
    from edarunner.cli import Router

HANDLE = re.compile(r"^[\w.@#/-]{1,128}$")
SOURCE = re.compile(r"^[\w.-]{1,64}$")
LOG_LINES = 200
# The words of the reply keyboard, in rows; a tap sends the word, which runs the command of that name.
KEYBOARD = (("Status", "Projects", "Hosts"), ("Events", "Tools", "Digest"))
# The placeholders of a custom command that need a project.
PROJECT_KEYS = ("{project}", "{root}", "{project_root}", "{handle}", "{run_id}", "{run_root}", "{host}")


@dataclass(frozen=True)
class Builtin:
    """One built-in command: its menu entry, its group in /help, and how its reply is sent."""

    name: str
    args: str
    help: str
    group: str
    kind: str = "text"  # "html" as it is, "pre" in a <pre> block, "text" escaped
    self_logged: bool = False  # the action writes its own database event
    on_run: bool = False  # the first argument is a handle; a reply to an alert fills it
    slow: bool = False  # it can take more than a second, so its message gets a reaction first


BUILTINS = {b.name: b for b in (
    Builtin("status", "[project|handle] [all]", "the board of every project, the board of one with all its runs, or "
                                                "one run", "Look", "html", on_run=True),
    Builtin("projects", "", "every project with its watcher and its live runs", "Look", "html"),
    Builtin("events", "[project] [n]", "the last events of every project or of one, newest first", "Look", "html"),
    Builtin("hosts", "", "the free room of every host and your runs on it", "Look", "html", slow=True),
    Builtin("tools", "", "seats used of total, and the hosts, per tool", "Look", "html"),
    Builtin("digest", "", "the daily digest now", "Look", "html"),
    Builtin("pin", "", "pin a new board message", "Look"),
    Builtin("log", "<handle> [n]", "the last n log lines as a file, default 200", "Files", on_run=True, slow=True),
    Builtin("board", "[project]", "compare.html and status.html as files", "Files"),
    Builtin("csv", "[project] <source>", "the metrics of one source as a CSV file", "Files"),
    Builtin("keep", "<handle> [hours]", "that many more hours on the budget, default 12, and no kill as hung and no "
                                        "stop as superseded for that long", "Act on a run", self_logged=True, on_run=True),
    Builtin("stop", "<handle> [why]", "stop after the running task", "Act on a run", self_logged=True, on_run=True),
    Builtin("compare", "<handle>...", "metrics side by side, runs of one project", "Compare", "pre"),
    Builtin("metric", "[project] <name> [--source SOURCE]", "one metric per run", "Compare", "pre"),
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
    """One answer to a command: the title of its first line, the body, how the body is sent, and files.

    `project` names the project in the first line; empty takes the name of the bot."""

    title: str
    body: str
    kind: str = "text"
    ok: bool = True
    documents: list[Document] = field(default_factory=list)
    markup: dict | None = None
    project: str = ""


def keyboard() -> dict:
    """The persistent reply keyboard of KEYBOARD."""
    return {"keyboard": [[{"text": w} for w in row] for row in KEYBOARD], "resize_keyboard": True, "is_persistent": True}


def keyboard_word(text: str) -> str | None:
    """The command of a keyboard word, such as `status` for `Status`; None for other text."""
    word = text.strip().lower()
    return word if any(word == w.lower() for row in KEYBOARD for w in row) else None


class Commands:
    """The built-in and custom commands of one bot, over every project of the router."""

    def __init__(self, router: Router, db: Any, custom: dict[str, BotCommand], site_dir: str,
                 repin: Callable[[], None]) -> None:
        self.router = router
        self.db = db
        self.table = custom
        self.site_dir = site_dir
        self.repin = repin
        self.here: str | None = None  # the project of the command: of the alert it replies to, or of its topic

    def menu(self) -> list[dict[str, str]]:
        """The `/` menu: every built-in, then every custom command."""
        out = [{"command": b.name, "description": (f"{b.help}: /{b.name} {b.args}" if b.args else b.help)[:256]}
               for b in BUILTINS.values()]
        return out + [{"command": n, "description": (c.help or n)[:256]} for n, c in self.table.items()]

    def slow(self, name: str) -> bool:
        """True for a command that can take more than a second: a custom one, or a slow built-in."""
        return name in self.table or (name in BUILTINS and BUILTINS[name].slow)

    def run(self, name: str, args: list[str], text: str, run: tuple[str, str] | None = None,
            topic: str | None = None) -> Reply:
        """Answer one command; an unknown name gets the help. Every command lands in the events.

        `run` is the (project, run id) of the alert the message replies to: it fills the handle of a
        built-in and the run placeholders of a custom command. `topic` is the project of the forum
        topic the message came from.
        """
        if name not in self.table and name not in BUILTINS:
            name = "help"
        self.here = run[0] if run else topic
        try:
            if name in self.table:
                return self.custom(self.table[name], args, run)
            b = BUILTINS[name]
            if run and b.on_run and args[:1] != [f"{run[0]}/{run[1]}"]:
                args = [f"{run[0]}/{run[1]}", *args]
            out = getattr(self, "cmd_" + name)(args)
            if not b.self_logged:
                self.event("command", text[:200])
            return out if isinstance(out, Reply) else Reply(name, out, b.kind)
        except Exception as e:  # a refused handle is an answer, not a crash
            self.event("refused", f"{text[:200]}: {e}")
            return Reply(name, f"error: {e}", ok=False)

    def custom(self, c: BotCommand, args: list[str], run: tuple[str, str] | None) -> Reply:
        """Run a custom command; one whose strings name the project or the run runs in the project the router picks."""
        env = {"user": getpass.getuser(), "site_dir": self.site_dir}
        log_dir, project = home.root(), ""
        texts = [*c.run, c.cwd or "{root}", *(c.skip_if or []), c.skip_reply, c.reply]
        if any(k in t for t in texts for k in PROJECT_KEYS):
            project, args = self.router.pick(args, self.here)
            with self.router.actions(project) as act:
                p = act.c.project
                root = str(p.root)
                env.update(project=p.project, root=root, project_root=root, site_dir=str(p.site.path.parent))
                log_dir = Path(p.data)
                if run and run[0] == project:
                    env.update(act.run_info(run[1]))
        text, ok = run_custom(c, args, env, log_dir, self.event)
        return Reply(c.name, text, "pre", ok, project=project)

    def event(self, kind: str, text: str) -> None:
        """One event with the actor telegram."""
        self.db.add_event(actor="telegram", run_id="", kind=kind, text=text)

    # built-in commands

    def _handle(self, args: list[str]) -> tuple[str, str]:
        """(project, handle) of the first argument; ValueError when it is no handle or names no single run."""
        if not args or not HANDLE.match(args[0]):
            raise ValueError("a handle is label@batch, project/label@batch, a run id prefix or #n")
        return self.router.resolve(args[0])

    def cmd_status(self, args: list[str]) -> Reply | str:
        """The board of every project, the board of one project with `all` its finished runs, or one run."""
        if not args and not self.here:
            return self.router.board_text()
        if not args or args[0] in self.router.names() or args[0] == "all":
            project, rest = self.router.pick(args, self.here)
            with self.router.actions(project) as act:
                return Reply("status", act.status_text(everything="all" in rest), "html", project=project)
        project, h = self._handle(args)
        with self.router.actions(project) as act:
            return Reply("status", act.status_text(h), "html", project=project)

    def cmd_projects(self, args: list[str]) -> str:
        """Every project with its watcher and its live runs."""
        return self.router.projects_text()

    def cmd_events(self, args: list[str]) -> Reply | str:
        """The last n events, default 8, at most 30, of every project or of the one named."""
        n = min(30, int(args[-1])) if args and args[-1].isdecimal() else 8
        project = args[0] if args and args[0] in self.router.names() else self.here
        if project:
            with self.router.actions(project) as act:
                return Reply("events", act.events_text(n), "html", project=project)
        return self.router.events_text(n)

    def cmd_hosts(self, args: list[str]) -> str:
        """One line per host."""
        return self.router.hosts_text()

    def cmd_tools(self, args: list[str]) -> str:
        """One line per tool."""
        return self.router.tools_text()

    def cmd_pin(self, args: list[str]) -> str:
        """Unpin the board and pin a new one."""
        self.repin()
        return "board pinned"

    def cmd_log(self, args: list[str]) -> Reply:
        """The last n lines, default 200, of the log of a run as a file `<handle>.log`."""
        project, h = self._handle(args)
        n = int(args[1]) if len(args) > 1 and args[1].isdecimal() else LOG_LINES
        with self.router.actions(project) as act:
            name, data = act.log_tail(h, n)
        return Reply("log", f"the last {n} lines", documents=[Document(name, data)], project=project)

    def cmd_board(self, args: list[str]) -> Reply | str:
        """compare.html and status.html of the last watcher cycle as files."""
        project, _ = self.router.pick(args, self.here)
        with self.router.actions(project) as act:
            files = act.board_files()
        if not files:
            return "no board files yet; the watcher writes them every cycle"
        return Reply("board", "", documents=[Document(p.name, p.read_bytes()) for p in files], project=project)

    def cmd_csv(self, args: list[str]) -> Reply | str:
        """The metrics of one source as `metrics.csv`."""
        project, args = self.router.pick(args, self.here)
        if not args or not SOURCE.match(args[0]):
            return "usage: /csv [project] <source>"
        with self.router.actions(project) as act:
            data = act.metrics_csv(args[0])
        return Reply("csv", args[0], documents=[Document("metrics.csv", data)], project=project)

    def cmd_digest(self, args: list[str]) -> str:
        """The daily digest now."""
        return self.router.digest_text()

    def cmd_keep(self, args: list[str]) -> Reply:
        """Give a run that many more hours, default 12, on its budget, and hold off the watcher for that long."""
        project, h = self._handle(args)
        hours = int(args[1]) if len(args) > 1 and args[1].isdigit() else 12
        with self.router.actions(project) as act:
            return Reply("keep", act.keep(h, hours, "telegram"), project=project)

    def cmd_stop(self, args: list[str]) -> Reply:
        """Stop a run after its running task."""
        project, h = self._handle(args)
        why = " ".join(args[1:]) or "stopped from telegram"
        with self.router.actions(project) as act:
            return Reply("stop", act.stop_after_task(h, "telegram", why), project=project)

    def cmd_compare(self, args: list[str]) -> Reply | str:
        """The metrics of several runs of one project side by side."""
        if not args:
            return "usage: /compare <handle>..."
        found = [self._handle([a]) for a in args]
        if len({p for p, _ in found}) > 1:
            raise ValueError("compare takes the runs of one project")
        with self.router.actions(found[0][0]) as act:
            return Reply("compare", act.compare_text([h for _, h in found]), "pre", project=found[0][0])

    def cmd_metric(self, args: list[str]) -> Reply | str:
        """One metric for every run of a project, or for the runs of one source."""
        project, args = self.router.pick(args, self.here)
        if not args:
            return "usage: /metric [project] <name> [--source SOURCE]"
        source = args[args.index("--source") + 1] if "--source" in args[:-1] else None
        with self.router.actions(project) as act:
            return Reply("metric", act.metric_text(args[0], source), "pre", project=project)

    def cmd_help(self, args: list[str]) -> str:
        """The built-in commands by group, then the custom commands."""
        groups: dict[str, list[tuple[str, str]]] = {}
        for b in BUILTINS.values():
            groups.setdefault(b.group, []).append((f"/{b.name} {b.args}", b.help))
        if self.table:
            groups["Custom"] = [(f"/{n} " + " ".join(f"<{a}>" for a in c.args), c.help or "")
                                for n, c in self.table.items()]
        return fmt.help_text(groups)

    def cmd_start(self, args: list[str]) -> Reply:
        """The help, with the reply keyboard."""
        return Reply("help", self.cmd_help(args), "html", markup=keyboard())

    def cmd_keyboard(self, args: list[str]) -> Reply:
        """Show the reply keyboard, or remove it with `off`."""
        if args[:1] == ["off"]:
            return Reply("keyboard", "keyboard off", markup={"remove_keyboard": True})
        return Reply("keyboard", "keyboard on", markup=keyboard())

"""The commands of the bot: the built-in table, one handler per built-in, and the custom argv commands.

A handler turns the arguments of one message into a `Reply`; the bot sends it.
"""

from __future__ import annotations

import getpass
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from edarunner.model import BotCommand, Project, Telegram
from edarunner.notify.telegram import format as fmt

if TYPE_CHECKING:
    from edarunner.cli import Actions

HANDLE = re.compile(r"^[\w.@#-]{1,128}$")


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


BUILTINS = {b.name: b for b in (
    Builtin("status", "[handle]", "the board, or one run", "Look", "html", on_run=True),
    Builtin("events", "[n]", "the last events, newest first", "Look", "html"),
    Builtin("hosts", "", "cores, RAM, scratch and GPUs, used of total", "Look", "html"),
    Builtin("lic", "", "licence seats, used of total", "Look", "html"),
    Builtin("board", "", "pin a new board message", "Look"),
    Builtin("keep", "<handle> [hours]", "add hours, default 12", "Act on a run", self_logged=True, on_run=True),
    Builtin("ack", "<handle>", "cancel a pending kill", "Act on a run", self_logged=True, on_run=True),
    Builtin("stop", "<handle> [why]", "stop after the running task", "Act on a run", self_logged=True, on_run=True),
    Builtin("compare", "<handle>...", "metrics side by side", "Compare", "pre"),
    Builtin("metric", "<name> [--design H]", "one metric per run", "Compare", "pre"),
    Builtin("help", "", "this list", "Compare", "html"),
)}


@dataclass
class Reply:
    """One answer to a command: the title of its first line, the body, and how the body is sent."""

    title: str
    body: str
    kind: str = "text"
    ok: bool = True


def handle(args: list[str]) -> str:
    """The first argument as a run handle; ValueError when it is not one."""
    if not args or not HANDLE.match(args[0]):
        raise ValueError("a handle is label@batch, a run id prefix or #n")
    return args[0]


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

    def run(self, name: str, args: list[str], text: str, run: str | None = None) -> Reply:
        """Answer one command; an unknown name gets the help. Every command lands in the ledger.

        `run` is the run of the alert the message replies to: it fills the handle of a built-in
        and the run placeholders of a custom command.
        """
        if name not in self.tg.commands and name not in BUILTINS:
            name = "help"
        try:
            if name in self.tg.commands:
                extra = self.actions.run_info(run) if run else {}
                return Reply(name, self.custom(self.tg.commands[name], args, extra), "pre")
            b = BUILTINS[name]
            if run and b.on_run and args[:1] != [run]:
                args = [run, *args]
            body = getattr(self, "cmd_" + name)(args)
            if not b.self_logged:
                self.event("command", text[:200])
            return Reply(name, body, b.kind)
        except Exception as e:  # a refused handle is an answer, not a crash
            self.event("refused", f"{text[:200]}: {e}")
            return Reply(name, f"error: {e}", ok=False)

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

    def cmd_lic(self, args: list[str]) -> str:
        """One line per licence."""
        return self.actions.lic_text()

    def cmd_board(self, args: list[str]) -> str:
        """Unpin the board and pin a new one."""
        self.repin()
        return "board pinned"

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

    # custom commands

    def custom(self, c: BotCommand, args: list[str], extra: dict[str, str] | None = None) -> str:
        """Run one `[telegram.commands.*]` entry: gate every argument, render the argv, run it without a shell.

        `extra` holds more placeholders, such as the run of the alert the command replies to.
        """
        names = list(c.args)
        if names and len(args) >= len(names):
            values = args[: len(names) - 1] + [" ".join(args[len(names) - 1 :])]  # the last argument takes the rest
        else:
            values = args
        if len(values) != len(names):
            return f"usage: /{c.name} " + " ".join(f"<{n}>" for n in names)
        project = self.project()
        root = str(project.root)
        env = {"project": project.project, "root": root, "project_root": root,
               "site_dir": str(project.site.path.parent), "user": getpass.getuser(), **(extra or {})}
        for n, v in zip(names, values):
            if not re.fullmatch(c.args[n], v):
                self.event("refused", f"/{c.name} {n}={v!r} does not match {c.args[n]}")
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
        self.event("command", f"/{c.name} " + " ".join(f"{n}={v}" for n, v in zip(names, values)))
        if c.dry_run:
            return f"would run in {cwd}:\n{shlex.join(argv)}"
        try:
            if skip and subprocess.run(skip, capture_output=True, cwd=cwd, timeout=c.timeout_s).returncode == 0:
                return skip_reply
            if c.detach:
                logf = Path(project.data) / f"telegram-{c.name}.log"
                logf.parent.mkdir(parents=True, exist_ok=True)
                with open(logf, "ab") as f:
                    p = subprocess.Popen(argv, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=f,
                                         stderr=subprocess.STDOUT)
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

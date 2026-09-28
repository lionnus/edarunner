"""The supervisor, `edr serve`: one process per user that keeps one watcher per registered project.

It holds `~/.edr/serve.lock` and runs a cycle every minute. It loads every registered project; a
project whose files do not load and that has no watcher of its own gets one alert per error text.
It keeps one `edr watch --served` per project in the project directory. A watcher that exits
starts again after 1, 2, 4, 8, 16 and at most 30 minutes, and the first exit of a series alerts.
A watcher whose `watch.json` stood still for max(3 heartbeat_s, 900 s) while its config loads is
killed and started again, with an alert. Then the supervisor does the work of the user
(`census.work`), edits the pinned global board, writes `serve.json` and tells systemd that it
lives. Every cycle starts from the files, so a restart of the supervisor loses nothing.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from . import __version__, analysis, board, census, config, home, metrics
from .guards import Refuse
from .hosts import HostProbe, floor
from .model import Placement, Project
from .notify import Notifier, alerts, make_notifiers
from .notify.telegram import format as tgfmt

if TYPE_CHECKING:
    from .cli import Router

log = logging.getLogger(__name__)

CYCLE_S = 60
BACKOFF_S = (60, 120, 240, 480, 960, 1800)
STUCK_S = 900  # a collect or a launch may take this long without a pulse of its watcher


def _notify(state: str) -> None:
    """Tell systemd `state`, READY=1 or WATCHDOG=1, when it runs this process as a notify service."""
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.sendto(state.encode(), "\0" + addr[1:] if addr.startswith("@") else addr)


def notifiers(router: Router | None = None) -> list[Notifier]:
    """The channels of the user file, with `edr` in the first line of each message and in the main thread of a
    forum; `router` lets the bot take the commands of every project, and the default site file adds its bot
    commands."""
    try:
        user = config.load_user()
    except config.ConfigError as e:
        log.warning("no channels: %s", e)
        return []
    try:
        site = config.load_site(config.DEFAULT_SITE)
    except config.ConfigError as e:
        log.warning("no site bot commands: %s", e)
        site = None
    me = SimpleNamespace(project="edr", root=home.root(), data=home.root())
    return make_notifiers(user, site, me, home.Store(), router, topic=False)  # type: ignore[arg-type]


@dataclass
class Child:
    """The watcher process of one project and its restarts."""

    proc: subprocess.Popen | None = None
    started: float = 0.0
    fails: int = 0
    wait_until: float = 0.0
    note: str = ""


def _watch(project: Project, once: bool = False) -> list[str]:
    return [sys.executable, "-m", "edarunner.cli", "watch", "--served", *(["--once"] if once else [])]


class Supervisor:
    """The watchers of every registered project, and the last config of each project that loaded."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers
        self.children: dict[str, Child] = {}
        self.good: dict[str, Project] = {}
        self.said: dict[str, str] = {}  # the text of the last alert per project and kind

    def _alert(self, kind: str, name: str, text: str, alert: alerts.Alert) -> None:
        """Send `alert` once per `text` for this project and kind."""
        if self.said.get(f"{kind}:{name}") != text:
            self.said[f"{kind}:{name}"] = text
            for n in self.notifiers:
                n.send(alert)

    def load(self) -> tuple[dict[str, Project], dict[str, str]]:
        """Every registered project that loads, and the error of each one that does not."""
        found, errors = {}, {}
        d = home.root() / "projects"
        for name in sorted(p.name for p in d.iterdir()) if d.is_dir() else []:
            path = home.owner(name)
            if path is None:
                continue
            try:
                found[name] = self.good[name] = config.load_project(path)
            except config.ConfigError as e:
                errors[name] = str(e)
        return found, errors

    def keep(self, name: str, found: dict[str, Project], errors: dict[str, str], now: float) -> None:
        """Start, watch and restart the watcher of project `name`."""
        c, p = self.children.setdefault(name, Child()), found.get(name) or self.good.get(name)
        if c.proc is not None and c.proc.poll() is None:
            assert p is not None
            last = max(float(config.load_json(p.state_dir / "watch.json").get("ts") or 0), c.started)
            if last > c.started:
                c.fails = 0
            if name in found and now - last > max(3 * p.limits.heartbeat_s, STUCK_S):
                self._stop(c)
                self._alert("stuck", name, str(c.started), alerts.served_alert(
                    p, "watcher stuck for", f"wrote no watch.json for {board.hm(now - last)}, so the supervisor killed "
                                           "it and starts it again"))
            return
        if c.proc is not None:
            code, c.proc = c.proc.returncode, None
            c.fails += 1
            c.wait_until = now + BACKOFF_S[min(c.fails, len(BACKOFF_S)) - 1]
            if c.fails == 1 and p is not None:
                self._alert("exit", name, str(now), alerts.served_alert(
                    p, "watcher exited for", f"exited with code {code}. The supervisor starts it again in a minute, and "
                                             "then after 2, 4, 8, 16 and at most 30 minutes while it keeps exiting"))
        if name in errors:
            c.note = errors[name]
            self._alert("config", name, errors[name], alerts.config_alert(
                p or SimpleNamespace(project=name, root=home.owner(name)), errors[name], watched=False))  # type: ignore[arg-type]
            return
        assert p is not None
        lock = p.state_dir / "watch.lock"
        if now < c.wait_until:
            return
        fd = home.lock(lock)
        if fd is None:
            c.note = f"watched by pid {home.holder(lock)}"
            return
        os.close(fd)
        c.note, c.started = "", now
        c.proc = subprocess.Popen(_watch(p), cwd=p.root, stdin=subprocess.DEVNULL)

    @staticmethod
    def _stop(c: Child) -> None:
        if c.proc is None:
            return
        c.proc.terminate()
        try:
            c.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            c.proc.kill()
            c.proc.wait()
        c.proc = None

    def stop(self) -> None:
        """Stop every watcher."""
        for c in self.children.values():
            self._stop(c)

    def cycle(self, now: float, once: bool = False) -> dict:
        """One cycle; with `once`, one `edr watch --once --served` per project instead of the watchers."""
        found, errors = self.load()
        for name in set(self.children) - set(found) - set(errors):
            self._stop(self.children.pop(name))
        for name in sorted({*found, *errors}):
            if once:
                if name in found:
                    subprocess.run(_watch(found[name], once=True), cwd=found[name].root, stdin=subprocess.DEVNULL)
                else:
                    self._alert("config", name, errors[name], alerts.config_alert(
                        SimpleNamespace(project=name, root=home.owner(name)), errors[name], watched=False))  # type: ignore[arg-type]
                continue
            self.keep(name, found, errors, now)
        t0, took = time.time(), None
        try:
            taken = census.work(self.notifiers, now)
            took = round(time.time() - t0, 1)
            text = global_board(found, taken, now)
            for n in self.notifiers:
                n.board(text)
        except Exception:  # the watchers go on without the census
            log.exception("census failed")
        kids = {n: self.children.get(n) or Child() for n in sorted({*found, *errors})}
        state = {"ts": now, "cycle": int(config.load_json(home.root() / "serve.json").get("cycle") or 0) + 1,
                 "pid": os.getpid(), "census_s": took,
                 "projects": {n: {"watcher": c.proc.pid if c.proc else None, "fails": c.fails, "note": c.note or errors.get(n, "")}
                              for n, c in kids.items()}}
        config.save_json(home.root() / "serve.json", state)
        return state


def global_board(found: dict[str, Project], taken: dict, now: float) -> str:
    """The global board: the runs of each project from its board.json, and the hosts that hold your runs."""
    rows = {name: config.load_json(p.data / "board" / "board.json").get("runs") or [] for name, p in found.items()}
    labels = {name: (metrics.step_totals(p), analysis.step_names(p)) for name, p in found.items()}
    sites = census.host_sites(found)
    probes = {h: p["error"] if "error" in p else HostProbe(**p) for h, p in (taken.get("hosts") or {}).items() if h in sites}
    view = census.host_view(probes, taken.get("runs") or [], {h: floor(sites[h], h) for h in probes}, Placement())
    # `#n` of the bot counts the runs in the order of this board.
    home.Store().set_store("last_board", [[name, r["run_id"]] for name in sorted(rows)
                                     for r in board.order(rows[name]) if board.is_live(r)])
    return tgfmt.global_board(rows, labels, [r for r in view if r["runs"] or r["note"]], now)


def run(notifiers: list[Notifier], once: bool = False) -> int:
    """The loop of the supervisor under `serve.lock`; 2 when another supervisor holds it."""
    path = home.root() / "serve.lock"
    fd = home.lock(path)
    if fd is None:
        print(f"edr serve: pid {home.holder(path)} serves already")
        return 2
    sup = Supervisor(notifiers)
    for n in notifiers:
        n.start()
    _notify("READY=1")
    try:
        while True:
            t0 = time.time()
            try:
                sup.cycle(t0, once)
            except Exception:  # the next cycle starts from the files again; the log keeps the traceback
                log.exception("serve cycle failed")
            _notify("WATCHDOG=1")
            if once:
                return 0
            time.sleep(max(1.0, CYCLE_S - (time.time() - t0)))
    finally:
        for n in notifiers:
            n.stop()
        sup.stop()
        os.close(fd)


def check(notifiers: list[Notifier], now: float | None = None) -> int:
    """1, with an alert, when `serve.json` is older than three cycles; it reads no project file."""
    s = config.load_json(home.root() / "serve.json")
    age = (now or time.time()) - float(s.get("ts") or 0)
    if s and age <= 3 * CYCLE_S:
        return 0
    print(f"edr serve: serve.json is {int(age)} s old (pid {s.get('pid')})" if s else "edr serve: no serve.json")
    a = alerts.serve_alert(age if s else None, s.get("pid"))
    for n in notifiers:
        n.send(a)
    return 1


def pinned() -> tuple[list[str], str, str]:
    """How to run a pinned copy: the command that installs one (none when this edarunner is no checkout),
    the `edr` that runs it, and a line that names it."""
    try:
        url = json.loads(importlib.metadata.distribution("edarunner").read_text("direct_url.json") or "{}")
    except importlib.metadata.PackageNotFoundError:
        url = {}
    if not (url.get("dir_info") or {}).get("editable"):
        # The edr that runs now: the first edr on PATH may be a checkout.
        exe = os.path.abspath(sys.argv[0]) if Path(sys.argv[0]).name == "edr" else shutil.which("edr") or sys.argv[0]
        return [], exe, f"edarunner {__version__} in {Path(exe).parent}"
    repo = url["url"].removeprefix("file://")
    commit = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if not commit or not shutil.which("uv"):
        raise Refuse(f"this edr runs from the checkout {repo}; install a copy at a commit with "
                     "uv tool install 'git+file://<repo>@<commit>' and run edr serve --unit from it")
    bin_dir = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True).stdout.strip()
    return (["uv", "tool", "install", "--force", f"git+file://{repo}@{commit}"], f"{bin_dir}/edr",
            f"edarunner at {commit} of {repo}, installed by uv tool install")


def unit(exe: str, what: str) -> str:
    """The systemd user unit that runs `edr serve` from `exe`."""
    return f"""# edr serve for every project of the user: {what}.
# Install it:
#   edr serve --unit > ~/.config/systemd/user/edr-serve.service
#   systemctl --user daemon-reload && systemctl --user enable --now edr-serve
# The unit survives a logout only after: loginctl enable-linger $USER
[Unit]
Description=edr serve for every project of %u
After=network-online.target

[Service]
Type=notify
NotifyAccess=main
# A user service starts with a bare PATH; ssh, rsync and git must be on it.
Environment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={shlex.quote(exe)} serve
Restart=always
RestartSec=30
WatchdogSec=600

[Install]
WantedBy=default.target
"""

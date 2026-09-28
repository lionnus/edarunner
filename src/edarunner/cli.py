"""The `edr` command line.

Every command wires the modules; nothing here knows a file format. Exit
codes: 0 done, 1 refused or bad input, 2 nothing to do, 3 some hosts
failed. `--json` prints {"code", "data", "output"} with the captured text.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import functools
import io
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from string import Template
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from . import __version__, analysis, board, brief, census, checkout, collect, config, export, home, launch, metrics, runid, serve, sync, watch
from .backend import Backend, Handle, Live, gone, make_backend, run_handle
from .config import ConfigError
from .db import Database, has_label, network_fs, pick
from .guards import Refuse, assert_run_id, assert_safe_target
from .hosts import HostError, HostProbe, Ssh, floor, probe_all
from .model import SCHEDULERS, Batch, Job, Placement, Project, Stage
from .notify import digest, make_notifiers, untag
from .notify.telegram import format as tgfmt
from .notify.telegram.bot import REPLY_DAYS

TEMPLATES = Path(__file__).resolve().parent / "templates"
Row = dict[str, Any]
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


# --- helpers

def _since(text: str) -> int:
    """'30m', '2h', '1d' or seconds, as the unix time that long ago."""
    try:
        unit = _UNIT.get(text[-1])
        secs = float(text[:-1]) * unit if unit else float(text)
    except (ValueError, IndexError):
        raise Refuse(f"--since {text!r}: use 30m, 2h, 1d or seconds") from None
    return int(time.time() - secs)


def _unix(text: str) -> int:
    """A unix time, or an ISO 8601 time, local unless it names its offset, as a unix time."""
    try:
        return int(float(text))
    except (ValueError, OverflowError):
        pass
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is neither a unix time nor an ISO time such as 2026-08-30T09:12") from None


def _param(text: str) -> tuple[str, str]:
    """KEY=VALUE as (key, value); config, build_tag and source have flags of their own."""
    key, sep, value = text.partition("=")
    if not sep or not key or key in ("config", "build_tag", "source"):
        raise argparse.ArgumentTypeError(f"{text!r} is not KEY=VALUE with a key other than config, build_tag and source")
    return key, value


def _check_phase(project: Project, phase: str) -> None:
    """Refuse a phase that the driver never ends a run with."""
    kind, sep, arg = phase.partition(":")
    n = re.fullmatch(r"(\d+)f(\d+)s", arg)
    stages = set(project.stages)
    if not {"done": not sep, "STOPPED": not sep, "KILLED": re.fullmatch(r"SIG[A-Z]+", arg),
            "INCOMPLETE": n and int(n[1]) + int(n[2]) > 0, "FAILED": arg in stages | {"setup", "runtime"},
            "OVER_BUDGET": arg in stages}.get(kind):
        raise Refuse(f"--phase {phase}: a run ends done, INCOMPLETE:<n>f<m>s with a task that failed or was skipped, "
                     "FAILED:<stage>, OVER_BUDGET:<stage>, STOPPED or KILLED:<signal>, with a stage of edr.toml; a run "
                     "whose driver died is FAILED:<stage>")


# These commands link the project into the registry, so the commands that span projects find it.
_REGISTERS = frozenset({"launch", "continue", "track", "import", "watch"})
# These commands never create data/edr.db; `notify` reads the bot's message ids only.
_READ_COMMANDS = frozenset({"brief", "status", "events", "hosts", "projects", "tools", "metrics", "compare", "runtime", "check",
                            "notify", "plan", "coverage"})
# These commands open the database read-only, so they write nothing, not even the -wal and -shm files of SQLite.
_READ_ONLY = frozenset({"events", "metrics", "compare", "runtime", "coverage"})


class Ctx:
    """Lazy project, db, ssh and backend of one invocation, plus the payload of --json."""

    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.data: Any = None
        self._project: Project | None = None
        self._db: Database | None = None

    @property
    def project(self) -> Project:
        if self._project is None:
            root = self.project_dir()
            if root is None:
                raise Refuse(f"no edr.toml in {os.getcwd()} or above; run edr init")
            self._project = config.load_project(root)
        return self._project

    @property
    def in_project(self) -> bool:
        """True when the command names a project or runs inside one."""
        return self._project is not None or self.project_dir() is not None

    def project_dir(self) -> Path | None:
        """The directory of the project -P names, else EDR_PROJECT, else the first edr.toml in the current directory or above.

        EDR_PROJECT is refused inside the directory of another project, so a shell variable never acts on the wrong one."""
        cwd = Path(os.getcwd())
        here = next((p for p in (cwd, *cwd.parents) if (p / "edr.toml").is_file()), None)
        pick = getattr(self.a, "project", None)
        name = pick or os.environ.get("EDR_PROJECT")
        if not name:
            return here
        target = home.owner(name)
        if target is None:
            raise Refuse(f"no registered project {name}; edr register in its directory adds it")
        if not pick and here is not None and Path(os.path.realpath(here)) != target:
            raise Refuse(f"EDR_PROJECT={name} is {target}, but {cwd} is inside the project {here}; pass -P {name} or unset it")
        return target

    @property
    def db(self) -> Database:
        if self._db is None:
            path = self.project.data / "edr.db"
            # A read command or a dry run creates nothing, not even an empty database.
            command = getattr(self.a, "command", None)
            memory = not path.exists() and (self.a.dry_run or command in _READ_COMMANDS)
            self._db = Database(":memory:") if memory else Database(path, readonly=command in _READ_ONLY)
        return self._db

    @functools.cached_property
    def ssh(self) -> Ssh:
        return Ssh(self.project.site)

    @functools.cached_property
    def backend(self) -> Backend:
        return make_backend(self.project.site, self.ssh)

    @functools.cached_property
    def console(self) -> Console:
        return board.console()

    def close(self) -> None:
        """Close the database when a command opened it."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def emit(self, out: str | RenderableType, data: Any = None) -> None:
        """Print `out`; keep `data` (default: the text) for --json."""
        self.data = board.plain(out) if data is None else data
        if self.a.json:
            return
        if isinstance(out, str):
            print(out)
        else:
            self.console.print(out)

    def batch_name(self, name: str | None) -> str:
        """`name`, else EDR_BATCH, else the newest batch directory of the state."""
        name = name or os.environ.get("EDR_BATCH")
        if name:
            return name
        dirs = [p for p in self.project.state_dir.glob("*") if p.is_dir() and p.name != "bin"]
        if not dirs:
            raise Refuse(f"no batch given and no batch directory in {self.project.state_dir}")
        return max(dirs, key=lambda p: p.stat().st_mtime).name

    def batch(self, name: str | None, dry_run: bool = False) -> Batch:
        """Load a batch, check its source out when it is missing, and resolve the source to its tag."""
        b = config.load_batch(self.project, self.batch_name(name))
        got = checkout.ensure(self.project, b.source, dry_run)
        if got:
            print(f"checkout {got.source} {got.path}" + (" (dry)" if dry_run else ""))
            b.source = got.source
        return self.resolve_source(b)

    def resolve_source(self, b: Batch) -> Batch:
        """A ref as source becomes the source tag of its checked-out tree; CheckoutError when it is not checked out."""
        if not runid.parse_tag(b.source, self.project.source.nested):
            b.source = runid.source_tag(checkout.find(self.project, b.source), self.project.source.nested)
        return b

    def refresh(self, batch: str | None = None) -> None:
        """Ingest the heartbeat files of the shown batches, so the board follows the driver, not the last watcher cycle.

        This is the watcher's own first step and idempotent, so a read command may run it.
        """
        heartbeats = watch.read_heartbeats(self.project, {batch} if batch else None)
        if heartbeats:
            watch.ingest(self.db, heartbeats)

    def rows(self, batch: str | None = None) -> list[Row]:
        """The runs of the batches that are not retired, with the state a heartbeat age gives."""
        self.refresh(batch)
        retired = {b["batch"] for b in self.db.batches() if b.get("retired")}
        rows = [r for r in self.db.runs(batch=batch) if r["batch"] not in retired]
        now, lim = time.time(), self.project.limits
        for r in rows:
            # The watcher's own classes (hung, host_full, ...) stay; only the age classes follow the heartbeat.
            if board.is_live(r) and r.get("updated") is not None and r.get("state") in (None, "running", "stale", "dead"):
                age = now - r["updated"]
                r["state"] = "dead" if age > lim.dead_s else "stale" if age > lim.stale_s else "running"
        return rows

    def resolve(self, handle: str) -> Row:
        """The database row of label@batch, label@source, a run id prefix, or #n from the last board."""
        try:
            run_id = self.db.resolve(handle, self.db.get_store("last_board"))
        except KeyError as e:
            raise Refuse(str(e)) from None
        row = self.db.run(run_id)
        if row is None:
            raise Refuse(f"{handle}: no run {run_id}")
        return row

    def target(self, handle: str) -> Row:
        """The row of the run a command acts on, after a line on stderr with its run id and phase, dry run or not."""
        row = self.resolve(handle)
        print(f"{row['run_id']}: phase {row.get('phase') or '-'}", file=sys.stderr)
        return row

    def heartbeat(self, row: Row) -> dict:
        """The heartbeat of a run, or {} when the driver wrote none."""
        return config.load_json(self.project.state_dir / str(row["batch"]) / f"{row['run_id']}.json")

    def left(self, row: Row, state: str) -> list[str]:
        """The stages that `edr continue` runs on the tree of a run that ended over its budget, for the triage; none
        for another run, or when continue refuses."""
        if state != "over_budget" or board.is_live(row):
            return []
        left, _, why = launch.stages_left(self.project, self.db, row)
        return [] if why else left

    def save_board(self, rows: list[Row]) -> None:
        """Keep the board order in the database, so #n resolves next time."""
        if rows:
            self.db.set_store("last_board", [r["run_id"] for r in board.order(rows)])


# --- texts shared by the commands and the bot

def _events_table(c: Ctx, events: list[Row]) -> Table | str:
    names = board.handles(c.db.runs())
    body = [[time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), e["actor"], names.get(e["run_id"], e["run_id"] or "-"),
             board.state_text(e["kind"]), e["text"]] for e in events]
    return board.table(["time", "actor", "run", "kind", "text"], body, styles={"time": "dim", "run": "bold"}) if body else "no events"


def _probe_rows(c: Ctx) -> list[Row]:
    """The probe of each site host, or its error."""
    return [asdict(p) if isinstance(p, HostProbe) else {"host": h, "error": p}
            for h, p in probe_all(c.ssh, c.project.site.hosts).items()]


def _host_rows(c: Ctx) -> list[Row]:
    """Every host of the site, probed now, with your live runs of every registered project; census.host_view has the rest.

    Outside a project it takes the default site file and the default [placement]."""
    found = census.projects()
    if c.in_project:
        found[c.project.project] = c.project
        site, pl = c.project.site, c.project.placement
    else:
        site, pl = config.load_site(config.DEFAULT_SITE), Placement()
    probes = probe_all(Ssh(site), site.hosts)
    return census.host_view(probes, census.live_runs(found.values(), time.time()), {h: floor(site, h) for h in probes}, pl)


def _yours(r: Row) -> str:
    return ", ".join(f"{p} {n}" for p, n in sorted(r["runs"].items())) or "-"


def _hosts_table(rows: list[Row], narrow: bool) -> RenderableType:
    """One row per host: whether a run can start, the free room of total, your runs and what they use; the notes under it."""
    head = ["ok", "host", "free cores", "free GB", "yours"] if narrow else [
        "ok", "host", "free cores", "free RAM GB", "free scratch GB", "idle GPUs", "tools", "your runs", "your cores",
        "your RAM GB", "your scratch GB"]
    body, notes = [], []
    for r in rows:
        mark = board.START[r["start"]]
        notes += [f"{r['host']}: {t}" for t in (r.get("error") or r["why"], r["note"]) if t]
        if "error" in r:
            body.append([mark, r["host"], Text("no answer", style="red")])
            continue
        cores, disk = f"{r['free_cores']:g}/{r['cores']}", f"{r['free_gb']:g}/{r['total_gb']:g}"
        if narrow:
            body.append([mark, r["host"], cores, disk, sum(r["runs"].values())])
            continue
        body.append([mark, r["host"], cores, f"{r['free_ram_gb']:g}/{r['total_ram_gb']:g}", disk,
                     f"{r['gpus_idle']}/{r['gpus']}" if r["gpus"] else "-", f"{r['our_tool_procs']}/{r['other_tool_procs']}",
                     _yours(r), f"{r['our_cores']:g}", f"{r['our_ram_gb']:g}", f"{r['our_gb']:g}"])
    right = ("free cores", "free GB", "yours", "free RAM GB", "free scratch GB", "idle GPUs", "tools", "your cores",
             "your RAM GB", "your scratch GB")
    t = board.table(head, body, styles={"host": "bold"}, right=right)
    if narrow:
        t.padding = (0, 0)  # 48 columns leave room for one space between columns
    return Group(t, Text("\n".join(notes))) if notes else t


def _seats(text: str) -> tuple[int, int | None] | None:
    """(free, total) from the first line of a probe, `free` or `free total`; None when it is no number."""
    parts = text.split("\n", 1)[0].split()
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else None
    except (IndexError, ValueError):
        return None


def _tool_rows(c: Ctx) -> list[Row]:
    """One row per site tool: the hosts that have it with their versions, and the seats its probe reports."""
    project = c.project
    # A probe may say {root}; from the head node the project directory stands in for it.
    values = config.placeholders(project, root=str(project.root), host="local")
    out: list[Row] = []
    for name, tool in project.site.tools.items():
        row: Row = {"tool": name, "hosts": {h.name: (h.tools or {}).get(name, "") for h in project.site.hosts.values()
                                            if h.has(name)}}
        if tool.seats is not None:
            row["total"] = tool.seats
        if not tool.probe:
            out.append(row)
            continue
        try:
            rc, text, err = c.ssh.run("local", [config.render(a, values) for a in tool.probe])
        except ConfigError as e:
            rc, text, err = 1, "", str(e)
        seats = _seats(text) if rc == 0 else None
        if seats is None:
            row["note"] = "unknown: " + (err.strip() or text.strip() or "no number").splitlines()[0]
        else:
            row["free"] = seats[0]
            if seats[1] is not None:
                row["total"] = seats[1]
        out.append(row)
    return out


def _tools_table(rows: list[Row]) -> Table | str:
    body = []
    for r in rows:
        free = Text(str(r["free"]), style="green" if r["free"] > 0 else "red") if "free" in r else None
        have = ", ".join(f"{h} {v}".strip() for h, v in r["hosts"].items())
        body.append([r["tool"], free, r.get("total"), have, Text(r["note"], style="red") if "note" in r else ""])
    return board.table(["tool", "free", "total", "hosts", "note"], body, styles={"tool": "bold"},
                       right=("free", "total")) if rows else "no tools"


def _metrics_table(rows: list[Row], root: Path) -> Table | str:
    """The metric rows with the file of each value and the snapshots of its run, those relative to `root`."""
    body = [[m.get("label"), m.get("source"), m["stage"], m.get("step"), m.get("task") or "", m["name"],
             analysis.mark(m["value"], m.get("verdict"), m.get("source_file")), m.get("unit"),
             "" if m["value"] is None else m.get("source_file"), ", ".join(os.path.relpath(s, root) for s in m["snapshots"])]
            for m in rows]
    return board.table(["label", "source", "stage", "step", "task", "metric", "value", "unit", "file", "snapshots"], body,
                       styles={"label": "bold", "source": "dim", "file": "dim", "snapshots": "dim"},
                       right=("step", "value")) if rows else "no metrics"


def _keep(c: Ctx, row: Row, hours: int, actor: str) -> str:
    """Write the keep file next to the spec: `hours` more on the budget, and the watcher waits that long."""
    run_id, note = row["run_id"], f"keep {hours} h"
    if not c.a.dry_run:
        config.save_json(c.project.state_dir / str(row["batch"]) / f"{run_id}.keep.json", {"hours": hours})
        c.db.add_event(actor, run_id, "keep", note)
    return note


class Actions:
    """The commands the Telegram bot may call; each one is a CLI command without the printing.

    The status, events, hosts and tools texts are Telegram HTML; the compare and metric texts are 40 plain columns.
    """

    def __init__(self, c: Ctx) -> None:
        self.c = c

    def run_info(self, handle: str) -> dict[str, str]:
        """The handle, run id, run root and host of one run, for the placeholders of a custom command."""
        row = self.c.resolve(handle)
        return {"handle": board.handle(row, self.c.db.runs()), "run_id": row["run_id"],
                "run_root": str(row.get("root") or ""), "host": str(row.get("host") or "")}

    def keep(self, handle: str, hours: int, actor: str) -> str:
        """`hours` more on the budget of the running stage or task, and no action of the watcher for that long."""
        row = self.c.resolve(handle)
        if not board.is_live(row):
            return f"{board.handle(row)} already {row['phase']}"
        return (f"{board.handle(row)}: " + _keep(self.c, row, hours, actor)
                + ", and no automatic stop or kill for as long unless its host runs out of scratch")

    def continue_run(self, handle: str, actor: str) -> str:
        """Run the stages the tree of a run has left as one new run, as edr continue without --stage does."""
        a = argparse.Namespace(**{**vars(self.c.a), "handle": handle, "stage": None, "tasks": None, "from_": None,
                                  "on": None, "parallel": None, "collect": None, "dry_run": False})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_continue(self.c, a, actor)
        return out.getvalue().strip()

    def free_space(self, handle: str, actor: str) -> str:
        """Remove the prune targets of the finished runs of the project on the host of a run, as edr retire --host
        does; a run whose tree has stages left keeps them."""
        row = self.c.resolve(handle)
        names = sorted({n for st in self.c.project.stages.values() for n in st.prune})
        if not names:
            return f"{self.c.project.project} declares no prune targets"
        a = argparse.Namespace(**{**vars(self.c.a), "handle": None, "batch": None, "host": row["host"],
                                  "prune": ",".join(names), "collect": None, "uncollected": False, "dry_run": False,
                                  "why": f"{row['host']} full: {actor}"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_retire(self.c, a)
        return (out.getvalue().strip() or f"no finished run of {self.c.project.project} with a tree on {row['host']}") + (
            "" if code == Exit.DONE else f"\nexit {int(code)}")

    def stop_after_task(self, handle: str, actor: str, why: str) -> str:
        c, row = self.c, self.c.resolve(handle)
        if not board.is_live(row):
            return f"{board.handle(row)} already {row['phase']}"
        launch.stop(c.ssh, c.db, row, {}, after_task=True, why=why, state=c.project.state_dir, actor=actor)
        return f"{board.handle(row)} stops after its task"

    def status_text(self, handle: str | None = None, everything: bool = False) -> str:
        """The board, with `everything` every finished run; or the state, stage, step, host, age, next command
        and last log line of one run."""
        if handle is None:
            rows = self.c.rows()
            self.c.save_board(rows)
            return tgfmt.board(rows, totals=metrics.step_totals(self.c.project),
                               names=analysis.step_names(self.c.project), everything=everything)
        row = self.c.resolve(handle)
        self.c.refresh(str(row["batch"]))
        row = self.c.db.run(row["run_id"]) or row
        return tgfmt.run_detail(row, self.c.heartbeat(row), time.time(), self.c.db.runs(), self.c.left(row, board.state_of(row)))

    def events_text(self, n: int) -> str:
        """The last `n` events, newest first."""
        return tgfmt.events(self.c.db.events(n=n), board.handles(self.c.db.runs()))

    def hosts_text(self) -> str:
        """One line per host, the hosts where a run can start first."""
        return tgfmt.hosts(_host_rows(self.c))

    def tools_text(self) -> str:
        """One line per tool."""
        return tgfmt.tools(_tool_rows(self.c))

    def log_tail(self, handle: str, n: int) -> tuple[str, bytes]:
        """The last `n` lines of the log of the running or last stage, fetched from the host: (file name, bytes)."""
        row = self.c.resolve(handle)
        log = self.c.heartbeat(row).get("log")
        if not log:
            raise Refuse(f"{board.handle(row)}: the heartbeat names no log")
        rc, out, err = self.c.ssh.run(str(row["host"]), ["tail", "-n", str(n), str(log)])
        if rc != 0:
            raise HostError(f"{row['host']}: tail {log}: rc {rc}: {err.strip()}")
        return f"{board.handle(row)}.log", out.encode()

    def board_files(self) -> list[Path]:
        """compare.html and status.html of the last watcher cycle, the ones that exist."""
        bdir = self.c.project.data / "board"
        return [p for p in (bdir / "compare.html", bdir / "status.html") if p.is_file()]

    def metrics_csv(self, source: str) -> bytes:
        """The CSV of `edr metrics --source <source> --csv`."""
        return _metrics_csv(analysis.mark_record(self.c.project, self.c.db.metrics(sources=[source]))).encode()

    def compare_text(self, handles: list[str]) -> str:
        """The rows of `edr compare` for one message, one block per metric and the metrics without a task first: the
        metric and its unit, then one `name value (stage step) percent` line per run, with the name of
        `analysis.names` and the percent against the first run. The runs that lack the step of record close the text,
        and the blocks that do not fit give way to a line `… N more rows`."""
        runs = [self.c.resolve(h) for h in handles]
        rows, missing = analysis.side_by_side(self.c.project, runs, self.c.db.metrics(run_ids=[r["run_id"] for r in runs]))
        col, first, blocks = analysis.names(runs), runs[0]["run_id"], []
        for r in rows:
            name, unit = r["metric"] + (f"[{r['task']}]" if r["task"] else ""), f" {r['unit']}" if r["unit"] else ""
            lines = board.cols(["", "", ""], [["  " + col[i], analysis.cell(r, i), "" if i == first else
                                              analysis._pct(r["value"].get(i), r["value"].get(first)) or ""] for i in col])
            blocks.append("\n".join([name[:40 - len(unit)] + unit, *lines.splitlines()[1:]]))
        return tgfmt.first_rows(blocks, analysis.missing_lines(missing)) or "no metrics"

    def compare_page(self, handles: list[str]) -> bytes:
        """compare.html over every run of the project, opened on the runs of `handles`."""
        return analysis.compare_page(self.c.project, self.c.db, [self.c.resolve(h)["run_id"] for h in handles]).encode()

    def metric_text(self, name: str, source: str | None) -> str:
        """`label source step value` per metric row."""
        rows = self.c.db.metrics(sources=[source] if source else None, name=name)
        body = [[str(m.get("label")) + (f"[{m['task']}]" if m.get("task") else ""), m.get("source"), m.get("step"),
                 f"{analysis.mark(m['value'], None, m.get('source_file'))}"
                 f"{' ' + m['unit'] if m.get('unit') and m['value'] is not None else ''}"] for m in rows]
        return board.cols(["label", "source", "step", "value"], body) if body else "no metrics"


# --- commands

def cmd_brief(c: Ctx, a: argparse.Namespace) -> int:
    """The project briefing, or the history of one run, as Markdown."""
    if a.run:
        row = c.resolve(a.run)
        row = next((r for r in c.rows(str(row["batch"])) if r["run_id"] == row["run_id"]), row)
        data = brief.run_data(c, row, c.backend.file_host(row))
        c.emit(brief.run_text(data), data)
        return Exit.DONE
    data = brief.project_data(c, _tool_rows(c))
    c.emit(brief.project_text(data), data)
    return Exit.DONE


def cmd_status(c: Ctx, a: argparse.Namespace) -> int:
    """The board, one run with its stages, metrics and log tail, or the daily digest."""
    if a.digest:
        text = _digest(census.projects() if a.all else {c.project.project: c.project})
        c.emit(untag(text), {"digest": text})
        return Exit.DONE
    if a.handle:
        row = c.resolve(a.handle)
        c.refresh(str(row["batch"]))
        row = c.db.run(row["run_id"]) or row
        hb, run_id = c.heartbeat(row), row["run_id"]
        stages = sorted(c.db.stage_runs(run_id), key=lambda r: (r["stage"] != "setup", r["stage"], r["task"], r["attempt"]))
        mets = c.db.metrics(run_ids=[run_id])
        samples, flags = c.db.run_samples(run_id), c.db.flags([run_id])
        c.emit(board.run_detail(row, stages, mets, str(hb.get("last_log") or ""), gate=hb.get("gate"), samples=samples,
                                flags=flags),
               {"run": row, "heartbeat": hb, "stages": stages, "metrics": mets, "samples": samples, "flags": flags})
        return Exit.DONE
    if a.all:
        rows = _all_rows(a)
        c.emit(board.wide(rows), {"runs": rows})
        return Exit.DONE
    if a.metric and (a.narrow or a.triage):
        raise Refuse("--metric adds columns to the wide board; drop --narrow and --triage")
    code = Exit.DONE
    while True:
        rows = [r for r in c.rows(a.batch or os.environ.get("EDR_BATCH")) if not a.source or r.get("source") in a.source]
        if a.live:
            code = max(code, _mark_live(c, rows))
        c.save_board(rows)
        mets, missing = _record_rows(c, rows, a.metric) if a.metric else ([], [])
        data = {"runs": rows, "metrics": mets, "missing": missing} if a.metric else {"runs": rows}
        lines = "\n".join(analysis.missing_lines(missing))
        if a.csv and not a.json:
            sys.stdout.write(_status_csv(rows, mets))
            if lines:
                print(lines, file=sys.stderr)
            c.data = data
            return code
        totals = metrics.step_totals(c.project)
        lacking = {(m["metric"], m["run_id"]) for m in missing}
        extra = {m["metric"]: {r["run_id"]: "missing" if (m["metric"], r["run_id"]) in lacking else analysis.cell(m, r["run_id"])
                               for r in rows} for m in mets}
        text = _triage(c, rows) if a.triage else board.narrow_text(rows, totals=totals) if a.narrow else board.wide(
            rows, totals=totals, extra=extra)
        if lines:
            text = Group(text, Text(lines, style="bold"))
        if a.watch and not a.json:
            c.console.clear()
        c.emit(text, data)
        if not a.watch or a.json:
            return code
        time.sleep(c.project.limits.heartbeat_s)


def _record_rows(c: Ctx, runs: list[Row], names: list[str]) -> tuple[list[Row], list[Row]]:
    """A row per metric of `names`, shaped as a row of `analysis.side_by_side`: the value, stage, step, source file and
    verdict of each run at its step of record, else at its own last step; and the runs that lack the step of record,
    each named by its handle.

    `analysis.pick_step` takes one run at a time, so a metric without `record` is at the last step of each run. A name
    matches a metric or a canonical name. The rows of a task are left out, since a task group has a value per task."""
    out, missing, everyone, ids = [], [], c.db.runs(), [r["run_id"] for r in runs]
    for name in names:
        by: dict[str, list[Row]] = {}
        for m in c.db.metrics(name=name, run_ids=ids):
            if not m.get("task"):
                by.setdefault(m["run_id"], []).append(m)
        found = sorted({m["name"] for ms in by.values() for m in ms})
        if len(found) > 1:
            raise Refuse(f"{name} names the metrics {board.join(found)}; pass one of them")
        row: Row = {"metric": found[0] if found else name, **{k: {} for k in ("value", "stage", "step", "source_file",
                                                                               "verdict")}}
        for r in runs:
            chosen, lack = analysis.pick_step(c.project, [r], by.get(r["run_id"], []))
            missing += [{**m, "label": board.handle(r, everyone)} for m in lack]
            for rid, m in chosen.items():
                for k in ("value", "stage", "step", "source_file"):
                    row[k][rid] = m.get(k)
                row["verdict"][rid] = analysis.verdict(c.project, m)
        out.append(row)
    return out, missing


def _status_csv(runs: list[Row], mets: list[Row]) -> str:
    """The board rows as CSV, with the value, stage, step and verdict of each metric of `_record_rows`."""
    head = ["run_id", "label", "batch", "source", "host", "state", "phase",
            *[f"{m['metric']}{k}" for m in mets for k in ("", "_stage", "_step", "_verdict")]]
    body = [[r["run_id"], r.get("label"), r.get("batch"), r.get("source"), r.get("host"), board.state_of(r), r.get("phase"),
             *[m[k].get(r["run_id"]) for m in mets for k in ("value", "stage", "step", "verdict")]] for r in board.order(runs)]
    return export.to_csv(head, body).decode()


def _all_rows(a: argparse.Namespace) -> list[Row]:
    """The board rows of every registered project, each with its project."""
    out = []
    for name, project in census.projects().items():
        other = Ctx(a)
        other._project = project
        try:
            out += [{**r, "project": name} for r in other.rows()]
        finally:
            other.close()
    return out


def _mark_live(c: Ctx, rows: list[Row]) -> int:
    """Ask the backend, once, whether the driver of each live run exists; a gone driver marks the run dead."""
    handles: dict[int, Handle] = {}
    for i, r in enumerate(rows):
        hb = c.heartbeat(r) if board.is_live(r) else {}
        if hb.get("driver_pid") and r.get("host") and (h := run_handle(c.backend.name, r, hb)):
            handles[i] = h
    alive = c.backend.alive(list(handles.values())) if handles else {}
    code = Exit.DONE
    for i, h in handles.items():
        state = alive[h][0]
        rows[i]["alive"] = None if state is Live.UNKNOWN else state is not Live.GONE
        if state is Live.UNKNOWN:
            code = Exit.HOSTS
        elif state is Live.GONE:
            rows[i]["state"] = "dead"
    return code


def _triage(c: Ctx, rows: list[Row]) -> Text | str:
    lines, everyone = [], c.db.runs()
    for r in board.order(rows):
        state = board.state_of(r)
        cmd = board.triage_cmd(r, state, c.heartbeat(r) if state == "dead" else {}, everyone, c.left(r, state))
        if cmd is None:
            continue
        lines.append(Text.assemble((f"{state:<11}", board.STYLE.get(state, "")), " ",
                                   (f"{board.handle(r, everyone):<28}", "bold"), f" {r.get('phase') or '-'}\n    {cmd}"))
    return Text("\n").join(lines) if lines else "nothing to triage"


def cmd_events(c: Ctx, a: argparse.Namespace) -> int:
    """The last events, filtered by time and run."""
    since = _since(a.since) if a.since else None
    run_id = c.resolve(a.run)["run_id"] if a.run else None
    events = c.db.events(since_s=since, run_id=run_id, n=a.n)
    c.emit(_events_table(c, events), events)
    return Exit.DONE if events else Exit.NOTHING


def cmd_hosts(c: Ctx, a: argparse.Namespace) -> int:
    """Probe every site host, or print the samples the watcher kept: over --since, or over the window of a batch."""
    if a.history or a.batch:
        since, until, runs = _window(c, a.batch) if a.batch else (_since(a.since), None, None)
        samples = [s for s in c.db.host_samples(since) if until is None or s["ts"] <= until]
        rows = [h for h in analysis.host_history(samples, a.batch or "", runs) if runs is None or h["host"] in runs]
        c.emit(analysis.host_history_view(rows), rows)
        return Exit.DONE if rows else Exit.NOTHING
    rows = _host_rows(c)
    if a.narrow:
        c.console.width = 48
    c.emit(_hosts_table(rows, a.narrow), rows)
    return Exit.HOSTS if any("error" in r for r in rows) else Exit.DONE


def _window(c: Ctx, batch: str) -> tuple[int, int, dict[str, list[list[Row]]]]:
    """The window of `batch`, from the start of its first run to the last heartbeat of its last run, or to now while a
    run lives, and the run samples of each of its runs, by host."""
    runs = [r for r in c.db.runs(batch=batch) if r.get("started")]
    if not runs:
        raise Refuse(f"no run of batch {batch} has started")
    end = time.time() if any(board.is_live(r) for r in runs) else max(r.get("updated") or r["started"] for r in runs)
    by: dict[str, list[list[Row]]] = {}
    for r in runs:
        by.setdefault(str(r.get("host") or ""), []).append(c.db.run_samples(r["run_id"]))
    return min(r["started"] for r in runs), int(end), by


def cmd_projects(c: Ctx, a: argparse.Namespace) -> int:
    """Every registered project: its directory, its watcher and its live runs."""
    d, now, rows = home.root() / "projects", time.time(), []
    for name in sorted(p.name for p in d.iterdir()) if d.is_dir() else []:
        path = home.owner(name)
        row: Row = {"project": name, "root": str(path or os.path.realpath(d / name)), "watcher": None, "live": None, "note": ""}
        rows.append(row)
        if path is None:
            row["note"] = "the link names no project; edr register in the right directory replaces it"
            continue
        try:
            p = config.load_project(path)
        except ConfigError as e:
            row["note"] = str(e)
            continue
        row["watcher"] = home.holder(p.state_dir / "watch.lock")
        row["live"] = len(census.live_runs([p], now))
    body = [[r["project"], r["root"], f"pid {r['watcher']}" if r["watcher"] else "-", r["live"], r["note"]] for r in rows]
    c.emit(board.table(["project", "directory", "watcher", "live", "note"], body, styles={"project": "bold"},
                       right=("live",)) if rows else "no registered project", rows)
    return Exit.DONE if rows else Exit.NOTHING


def cmd_tools(c: Ctx, a: argparse.Namespace) -> int:
    """Every site tool: the seats its probe reports from the head node, and the hosts that have it."""
    rows = _tool_rows(c)
    c.emit(_tools_table(rows), rows)
    return Exit.HOSTS if any("note" in r for r in rows) else Exit.DONE


def _metrics_csv(rows: list[Row]) -> str:
    """Metric rows as CSV with the columns of an export; `analysis.mark_record` sets their `record`."""
    return export.to_csv(export.METRIC_COLUMNS, [export.metric_row(m) for m in rows]).decode()


def _instances_table(rows: list[Row]) -> Table | str:
    head = ["label", "source", "stage", "step", "task", "metric", "part", "instance", "depth", "value", "local", "cells", "unit"]
    body = [[m.get("label"), m.get("source"), m["stage"], m.get("step"), m["task"], m["name"], m["part"], m["instance"],
             m["depth"], board.num(m["value"]), board.num(m.get("local")), m.get("cells"), m.get("unit")] for m in rows]
    return board.table(head, body, styles={"label": "bold", "source": "dim"},
                       right=("step", "depth", "value", "local", "cells")) if rows else "no instance rows"


def cmd_metrics(c: Ctx, a: argparse.Namespace) -> int:
    """The metrics of the chosen runs, as a table, CSV or a pivot of one metric; with --instance or --depth, their
    instance rows."""
    if a.instance is not None or a.depth is not None:
        run_ids = [c.resolve(a.run)["run_id"]] if a.run else None
        rows = c.db.instances(run_ids=run_ids, sources=a.source, stage=a.stage, step=a.step, name=a.metric,
                              depth=a.depth, instance=a.instance)
        analysis.to_ge(c.project, a.unit, rows, ("value", "local"))
        c.emit(_instances_table(rows), rows)
        return Exit.DONE if rows else Exit.NOTHING
    if not (a.source or a.run or a.label or a.task):
        raise Refuse("metrics needs --source, --run, --label or --task")
    run_ids = [c.resolve(a.run)["run_id"]] if a.run else None
    if a.over:
        if not run_ids or a.unit:
            raise Refuse("--over steps needs --run and takes no --unit")
        rows = analysis.over_steps(c.project, c.db.metrics(run_ids=run_ids, stage=a.stage, name=a.metric))
        c.emit(analysis.over_steps_view(rows), rows)
        return Exit.DONE if rows else Exit.NOTHING

    def chosen(r: Row) -> bool:
        return ((not a.source or r["source"] in a.source) and (run_ids is None or r["run_id"] in run_ids)
                and (not a.label or has_label(r, a.label, c.project.stages)))

    # The step of record needs every step of a run, so --step filters after the mark.
    rows = c.db.metrics(sources=a.source, stage=a.stage, name=a.metric, run_ids=run_ids, task=a.task)
    rows = [{**m, "verdict": analysis.verdict(c.project, m)} for m in analysis.mark_record(c.project, [m for m in rows if chosen(m)])
            if a.step is None or m.get("step") == a.step]
    analysis.to_ge(c.project, a.unit, rows)
    if a.pivot:
        if len(have := sorted({m["name"] for m in rows})) > 1:
            raise Refuse(f"--pivot takes one metric; name it with --metric: {', '.join(have)}")
        p = analysis.pivot(c.project, c.db, [r for r in c.db.runs() if chosen(r)], rows)
        if a.csv and not a.json:
            sys.stdout.write(export.to_csv(["label", "source", *p["columns"]],
                                           [[r["label"], r["source"], *r["cells"].values()] for r in p["rows"]]).decode())
            c.data = p
        else:
            c.emit(analysis.pivot_view(p), p)
    elif a.csv and not a.json:
        sys.stdout.write(_metrics_csv(rows))
        c.data = rows
    else:
        snaps = analysis.snapshots(c.project, c.db)
        for m in rows:
            m["snapshots"] = snaps.get(m["run_id"], [])
        c.emit(_metrics_table(rows, c.project.root), rows)
    return Exit.DONE if rows else Exit.NOTHING


def cmd_extract(c: Ctx, a: argparse.Namespace) -> int:
    """Extract every configured metric again from the collected files of runs, and make the rows of each run match."""
    if sum(map(bool, (a.handle, a.batch, a.source))) != 1:
        raise Refuse("extract needs one of a handle, --batch or --source")
    if a.handle:
        runs = [c.resolve(a.handle)]
    else:
        runs = [r for r in c.db.runs(batch=a.batch) if not a.source or r["source"] in a.source]
    out, lines = [], []
    inst = ("part", "instance", "depth", "value", "local", "cells")
    clashes = analysis.field_clashes(c.db.task_fields())
    for run in runs:
        old = {(m["stage"], m["step"], m["task"], m["name"]): m for m in c.db.metrics(run_ids=[run["run_id"]])}
        held: dict[tuple, set] = {}
        for x in c.db.instances(run_ids=[run["run_id"]]):
            held.setdefault((x["stage"], x["step"], x["task"], x["name"]), set()).add(tuple(x[k] for k in inst))
        n = dict.fromkeys(("new", "changed", "unchanged", "failed", "removed", "kept"), 0)
        hb = c.heartbeat(run)
        rows = watch.extract_run(c.project, c.db, run, hb, actor=None if a.dry_run else "user")
        write = []
        for r in rows:
            key = (r["stage"], r["step"], r["task"], r["name"])
            m = old.pop(key, None)
            same = (m is not None and all(m[k] == r[k] for k in ("value", "canonical", "unit", "source_file"))
                    and {tuple(i[k] for k in inst) for i in r.get("instances") or []} == held.get(key, set()))
            n["failed" if r["value"] is None else "new" if m is None else "unchanged" if same else "changed"] += 1
            if not same:
                write.append(r)
        # A row the extraction no longer gives goes when it failed, when no metric defines it, or when the extraction
        # read its stage and task. A value stays when its file is gone, and when the extraction did not read it: a
        # stage outside the run's spec, or a task that tasks.toml no longer resolves.
        results, read, drop = c.project.data / "results" / run["run_id"], {(r["stage"], r["task"]) for r in rows}, []
        for m in old.values():
            if m["value"] is not None and not (results / metrics.source_path(m["source_file"])).is_file():
                n["kept"] += 1
            elif m["value"] is None or (m["stage"], m["task"]) in read or not metrics.defines(c.project, m):
                drop.append(m)
        n["removed"] = len(drop)
        failed = metrics.failures(rows)
        text = (f"{n['new']} new, {n['changed']} changed, {n['unchanged']} unchanged, {n['failed']} failed, "
                f"{n['removed']} removed" + (f", {n['kept']} kept without a file" if n["kept"] else ""))
        flags: list[Row] = []
        if not a.dry_run:
            with c.db.conn:  # one commit for the run
                for r in write:
                    c.db.add_metric(r, replace=True)
                c.db.remove_metrics(drop)
                flags = watch.check_run(c.project, c.db, run, hb)
                k = sum(p["origin"] == "extract" for p in c.db.parameters(run["run_id"]))
                text += (f", {k} parameter{'s' * (k != 1)}" if k else "") + (
                    f", {len(flags)} flag{'s' * (len(flags) != 1)}" if flags else "")
                c.db.add_event("user", run["run_id"], "extract", text + (f"; {metrics.failure_text(failed)}" if failed else ""))
        out.append({"run_id": run["run_id"], **n, "failures": failed, "flags": flags,
                    "clashes": [x for x in clashes if any(run["run_id"] in s["runs"] for s in x["sets"])]})
        lines.append(f"{run['run_id']}: {text}" + (" (dry)" if a.dry_run else ""))
        if failed:
            lines.append("  " + metrics.failure_text(failed, "\n  "))
        lines += [f"  flag {f['check']}{' ' + f['task'] if f['task'] else ''}: {f['text']}" for f in flags]
    names = board.handles(c.db.runs())
    lines += [f"warning: {analysis.clash_text(x, names)}" for x in clashes if any(x in o["clashes"] for o in out)]
    c.emit("\n".join(lines) or "no runs", out)
    return Exit.DONE if runs else Exit.NOTHING


def cmd_compare(c: Ctx, a: argparse.Namespace) -> int:
    """Two or more runs side by side, each at its step of record, under a line that names the sources when they
    differ and a table of the parameters that differ; with --instances, the instances of one metric; with --ref or
    --base, the runs as rows with their rank."""
    if not a.instances and (a.csv or any(v is not None for v in (a.part, a.depth, a.instance))):
        raise Refuse("--part, --depth, --instance and --csv need --instances")
    runs = [c.resolve(h) for h in a.handles]
    ids = [r["run_id"] for r in runs]
    marks = {k: c.resolve(h) for k, h in (("ref", a.ref), ("base", a.base)) if h}
    if marks and a.instances:
        raise Refuse("--ref and --base compare metrics, not --instances")
    # A run of --ref or --base that is not a handle gives the percent and gets no row.
    runs += {r["run_id"]: r for r in marks.values() if r["run_id"] not in ids}.values()
    sources = sorted({str(r.get("source")) for r in runs})
    mixed = len(sources) > 1
    params = analysis.parameters_differ(c.db, runs)

    def shown(view: RenderableType) -> RenderableType:
        head = [Text(f"mixed sources: {', '.join(sources)}", style="yellow")] if mixed else []
        head += [analysis.parameters_view(runs, params)] if params else []
        return Group(*head, view) if head else view

    if a.instances:
        have = c.db.instance_names([r["run_id"] for r in runs])
        want = [n for n in have if not a.metric or {n, getattr(c.project.metrics.get(n), "canonical", n)} & set(a.metric)]
        if not have:
            c.emit("no instance rows", {"runs": [], "rows": []})
            return Exit.NOTHING
        if len(want) != 1:
            raise Refuse(f"name one instance metric with --metric: {', '.join(have)}")
        view = analysis.instance_delta(c.project, c.db, runs, want[0], a.task or "", a.part, 1 if a.depth is None else a.depth,
                                       a.instance, a.stage, a.step)
        if a.unit:
            if view["unit"] != "um2":
                raise Refuse(f"--unit {a.unit} converts um2, and {want[0]} is in {view['unit'] or 'no unit'}")
            analysis.to_ge(c.project, a.unit, [*view["runs"], *view["rows"]], ("value", "delta"))
            view["unit"] = a.unit
        rows, missing = view["rows"], view["missing"]
        if a.csv and not a.json:
            body = [[t["instance"], *[t["value"][p["run_id"]] for p in view["runs"]]] for t in rows]
            sys.stdout.write(export.to_csv(["instance", *analysis.run_heads(view["runs"])], body).decode())
            c.data = view
        else:
            c.emit(shown(analysis.instances_view(view)), {**view, "mixed_sources": mixed, "parameters": params})
    else:
        mets = [m for m in c.db.metrics(run_ids=[r["run_id"] for r in runs], stage=a.stage, task=a.task)
                if not a.metric or m["name"] in a.metric or m.get("canonical") in a.metric]
        rows, missing = analysis.side_by_side(c.project, runs, mets, a.stage, a.step)
        analysis.to_ge(c.project, a.unit, rows, ("value", "delta"))
        data = {"runs": runs, "rows": rows, "missing": missing, "mixed_sources": mixed, "parameters": params}
        if not marks:
            c.emit(shown(analysis.side_by_side_view(runs, rows, missing)), data)
        elif len({r["metric"] for r in rows}) < len(rows):
            raise Refuse("--ref and --base need one value per metric of each run; narrow the rows with --task, --metric "
                         "or --stage")
        else:
            ref, base = marks.get("ref", runs[0])["run_id"], marks["base"]["run_id"] if "base" in marks else None
            differ = analysis.ranked(rows, ids, ref, base)
            c.emit(shown(analysis.ranked_view(runs, ids, rows, ref, base, differ, missing)),
                   {**data, "ref": ref, "base": base, "ranks_differ": differ})
    if a.html:
        page = Path(a.html)
        config.save_text(page, analysis.compare_page(c.project, c.db, ids, board.plotly_src(page.parent)))
        print(f"wrote {page}", file=sys.stderr)
    return Exit.DONE if rows and not missing else Exit.NOTHING


def cmd_runtime(c: Ctx, a: argparse.Namespace) -> int:
    """Stage, step and task times of one run, or a table of runs."""
    if a.batch:
        runs = c.db.runs(batch=a.batch)
    elif a.handles:
        runs = [c.resolve(h) for h in a.handles]
    else:
        raise Refuse("runtime needs a handle or --batch")
    rts = [analysis.runtime(c.project, c.db, r) for r in runs]
    if len(rts) == 1 and not a.batch:
        c.emit(analysis.runtime_view(rts[0]), rts[0])
    else:
        c.emit(analysis.runtime_batch_view(c.project, rts), rts)
    return Exit.DONE if any(rt["stages"] or rt["steps"] for rt in rts) else Exit.NOTHING


def cmd_init(c: Ctx, a: argparse.Namespace) -> int:
    """Write edr.toml into the current directory."""
    path = Path(os.getcwd()) / "edr.toml"
    if path.exists():
        raise Refuse(f"{path} exists; edit it or remove it first")
    site = Path(a.site).expanduser()
    if site.suffix != ".toml":
        site = site / "site.toml"
    text = Template((TEMPLATES / "edr.toml").read_text()).substitute(project=path.parent.name, site=str(site))
    if not a.dry_run:
        path.write_text(text)
    c.emit(f"write {path}" + (" (dry)" if a.dry_run else ""), {"written": [str(path)], "site": str(site)})
    return Exit.DONE


def cmd_check(c: Ctx, a: argparse.Namespace) -> int:
    """Load every file, probe the hosts, import the hooks, plan every batch."""
    problems: list[str] = []
    try:
        project = c.project
    except ConfigError as e:
        c.emit(f"problem: {e}", {"problems": [str(e)]})
        return Exit.REFUSED
    hooks = [project.source.build_tag, project.task_resolver, project.checks.python,
             *(m.python for m in (*project.metrics.values(), *project.parameters.values()))]
    for spec in filter(None, hooks):
        try:
            config.load_hook(project.root, spec)
        except ConfigError as e:
            problems.append(f"hook {spec}: {e}")
    if not launch.DRIVER_SRC.is_file():
        problems.append(f"driver missing: {launch.DRIVER_SRC}")
    try:
        config.load_user()
    except ConfigError as e:
        problems.append(str(e))
    batches: list[Batch] = []
    for f in sorted((project.root / "jobs").glob("*.toml")):
        try:
            b = config.load_batch(project, str(f))
        except ConfigError as e:
            problems.append(str(e))
            continue
        # The same source tag as plan; check runs before checkout, so a ref that is not checked out stays as written.
        with contextlib.suppress(checkout.CheckoutError):
            c.resolve_source(b)
        batches.append(b)
    hosts = _probe_rows(c)
    problems += [f"{r['host']}: {r['error']}" for r in hosts if "error" in r]
    sched = project.site.scheduler.backend in SCHEDULERS
    warnings = []
    for name in sorted({t.licence for t in project.site.tools.values() if t.licence}) if sched else []:
        known = c.backend.licence(name)
        if known is False:
            problems.append(f"licence {name}: the scheduler does not define it, so it would never hold a job back")
        elif known is None:
            warnings.append(f"licence {name}: cannot ask {project.site.scheduler.backend} whether it is defined")
    problems += [p for p in c.ssh.check_local() if p not in problems]
    if why := home.clash(project.project, project.root):
        problems.append(why)
    probes = {r["host"]: HostProbe(**r) for r in hosts if "error" not in r}
    for b in batches:
        bad = [f"{b.batch}: job {j.label} names unknown host {j.host}" for j in b.jobs
               if j.host != "auto" and j.host not in project.site.hosts and not sched]
        problems += bad
        if not bad:
            for p in launch.plan(project, b, c.ssh, c.db, probes=probes):
                problems += [f"{b.batch}/{p.label}: {x}" for x in p.problems]
    text = Text("\n").join(Text.assemble(("problem", "red"), f": {p}") for p in problems) if problems else Text.assemble(
        ("ok", "green"), f": {len(hosts)} hosts, {len(project.stages)} stages, {len(project.metrics)} metrics, {len(batches)} batches")
    db_path = project.data / "edr.db"
    if fs := network_fs(db_path.parent):
        warnings.append(f"{db_path} is on a network filesystem ({fs}); journal_mode DELETE, not WAL")
    names = board.handles(c.db.runs())
    warnings += [analysis.clash_text(x, names) for x in analysis.field_clashes(c.db.task_fields())]
    text = Text("\n").join([*(Text.assemble(("warning", "yellow"), f": {w}") for w in warnings), text])
    c.emit(text, {"problems": problems, "warnings": warnings, "hosts": hosts, "batches": [b.batch for b in batches]})
    return Exit.REFUSED if problems else Exit.DONE


def cmd_checkout(c: Ctx, a: argparse.Namespace) -> int:
    """Check out a ref as a clone, or copy a dirty tree as a snapshot."""
    res = checkout.checkout(c.project, a.ref, Path(a.dirty) if a.dirty else None, a.dry_run)
    c.emit(Text.assemble((res.source, "bold"), " ", (str(res.path), "dim"), (" (dirty)" if res.dirty else "", "yellow")),
           {"source": res.source, "path": str(res.path), "nested": res.nested, "dirty": res.dirty})
    return Exit.DONE


def cmd_plan(c: Ctx, a: argparse.Namespace) -> int:
    """Render every job of a batch; writes nothing."""
    plans = launch.plan(c.project, c.batch(a.batch, a.dry_run), c.ssh, c.db, backend=c.backend)
    lines = [Text.assemble((p.run_id, "bold"), ": ",
                           (p.host or ("queued" if p.queued else c.project.site.scheduler.backend), "cyan" if p.queued else ""), " ", (str(p.root), "dim"),
                           *[Text.assemble("\n    ", ("problem", "red"), f": {x}") for x in p.problems]) for p in plans]
    c.emit(Text("\n").join(lines),
           [{"run_id": p.run_id, "label": p.label, "host": p.host, "root": p.root, "queued": p.queued,
             "problems": p.problems, "spec": p.spec} for p in plans])
    if a.show_spec:
        print("\n\n".join(launch.spec_text(c.project, p.spec) for p in plans if p.spec))
    return Exit.REFUSED if any(p.problems for p in plans) else Exit.DONE


def cmd_launch(c: Ctx, a: argparse.Namespace) -> int:
    """Start one driver per job of a batch."""
    only = a.only.split(",") if a.only else None
    rows = launch.launch(c.project, c.batch(a.batch, a.dry_run), c.ssh, c.db, dry_run=a.dry_run, only=only,
                         allow_dirty=a.allow_dirty, backend=c.backend)
    if a.show_spec:
        print("\n\n".join(launch.spec_text(c.project, r["spec"]) for r in rows if r["spec"]))
    started, queued = sum(r["started"] for r in rows), sum(r["queued"] for r in rows)
    problems = [r["problems"] for r in rows if r["problems"]]
    c.emit(Text.assemble((f"{started} started", "green" if started else ""), ", ", (f"{queued} queued", "cyan" if queued else ""),
                         ", ", (f"{len(problems)} with problems", "red" if problems else ""), " (dry)" if a.dry_run else ""), rows)
    if started or queued or (a.dry_run and not problems):
        return Exit.DONE
    # A second launch of the same batch names every old job "already launched"; that is nothing to do.
    return Exit.NOTHING if all(any(p.startswith("already launched") for p in ps) for ps in problems) else Exit.REFUSED


def cmd_continue(c: Ctx, a: argparse.Namespace, actor: str = "user") -> int:
    """Run stages on the tree of an existing run, by default the stages its tree has left, or fetch a
    collect_on_request list."""
    row = c.target(a.handle)
    project, run_id, h = c.project, row["run_id"], board.handle(row, c.db.runs())
    if a.collect:
        res = collect.collect_on_request(project, c.ssh, c.db, row, a.collect, a.dry_run)
        if not a.dry_run:
            c.db.add_event("user", run_id, "collect", f"{a.collect}: {res.files} files, {len(res.failures)} failed")
        c.emit("\n".join([f"{run_id}: {res.files} files" + (" (dry)" if a.dry_run else ""), *res.failures]), asdict(res))
        return Exit.HOSTS if res.failures else Exit.DONE
    stages, held = a.stage, []
    if not stages:
        stages, held, why = launch.stages_left(project, c.db, row)
        if why:
            raise Refuse(f"{h}: {why}")
        if not stages:
            c.emit(f"{h}: no stage left on its tree")
            return Exit.NOTHING
    if unknown := [s for s in stages if s not in project.stages]:
        raise Refuse(f"unknown stage {unknown[0]}")
    try:
        jobs = {j.label: j for j in config.load_batch(project, str(row["batch"])).jobs}
    except (ConfigError, OSError):
        jobs = {}  # an imported tree has no jobs file; the database row is the job
    # A continued run is <label>.<suffix> in the batch of the run it continues, and takes the job of that label.
    label = str(row["label"])
    while label not in jobs and "." in label:
        label = label.rsplit(".", 1)[0]
    job = jobs.get(label) or Job(label=str(row["label"]), config=str(row.get("config") or ""))
    job.reuse, job.stages, job.host = {"run_id": run_id}, stages, a.on or "auto"
    if a.tasks:
        job.tasks, held = a.tasks, []
    # The first stage runs its held tasks alone, and a later task group every task of the job.
    job.tasks = [*job.tasks, *(t for t in held if t not in job.tasks)]
    for s in stages if a.parallel else []:
        project.stages[s].parallel = a.parallel
    # The run joins the batch of the tree it continues; its label and the time keep its id apart.
    job.label = base = f"{row['label']}.{stages[0]}" + (f"-{stages[-1]}" if len(stages) > 1 else "")
    taken, n = {r["label"] for r in c.db.runs(batch=str(row["batch"]))} if len(stages) > 1 else set(), 1
    while job.label in taken:
        n += 1
        job.label = f"{base}.{n}"
    batch = Batch(batch=str(row["batch"]), source=str(row["source"] or ""), jobs=[job],
                  path=project.root / "jobs" / f"{row['batch']}.toml")
    state = project.state_dir
    (p,) = launch.plan(project, batch, c.ssh, c.db, date=time.strftime(launch.DATE_FMT), backend=c.backend,
                       reserve=not a.dry_run)
    if p.problems:
        c.emit("\n".join(f"{p.run_id}: problem: {x}" for x in p.problems), {"problems": p.problems})
        return Exit.REFUSED
    if held:
        p.spec["stages"][0]["tasks"] = [t for t in p.spec["stages"][0]["tasks"] if t["id"] in held]
    if a.from_:
        if not p.spec["stages"][0].get("resume"):
            raise Refuse(f"stage {stages[0]} has no resume command; --from needs one")
        p.spec["start_at"]["checkpoint"] = a.from_
    driver = sync.publish_driver(state, launch.DRIVER_SRC, a.dry_run)
    spec_path = launch.write_spec(state, batch.batch, p, driver, a.dry_run)
    c.emit(f"{p.run_id}: {board.left_text(stages, held)} on {p.host} {p.root}" + (" (dry)" if a.dry_run else ""),
           {"run_id": p.run_id, "batch": batch.batch, "host": p.host, "root": p.root, "spec": p.spec})
    if a.dry_run:
        return Exit.DONE
    c.db.upsert_run({**launch.run_row(p, batch), "phase": "setup", "state": "running", "started": int(time.time())})
    c.db.add_event(actor, p.run_id, "continue", f"{board.left_text(stages, held)} on {run_id}" + (f" from {a.from_}" if a.from_ else ""))
    launch.submit(c.backend, c.db, project, p.run_id, p.host, spec_path, driver)
    return Exit.DONE


def cmd_track(c: Ctx, a: argparse.Namespace) -> int:
    """Record a command as a run of the project, then replace this process with the driver of that run."""
    argv = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not argv:
        raise Refuse("track needs a command after --")
    project = c.project
    stage = project.stages.get(a.stage) or Stage(name=a.stage)
    if stage.is_group:
        raise Refuse(f"stage {a.stage} is a task group; track runs one command")
    root = Path(a.root or os.getcwd()).resolve()
    if not root.is_dir():
        raise Refuse(f"'{root}' is not a directory")
    try:
        source = a.source or runid.source_tag(root, project.source.nested)
    except runid.GitError as e:
        raise Refuse(f"no source tag for {root}: {e}; pass --source") from None
    date, host = time.strftime(launch.DATE_FMT), socket.gethostname()
    job = Job(label=a.label, config=a.config or a.label)
    batch = Batch(batch=a.batch, source=source, jobs=[job], path=project.root / "jobs" / f"{a.batch}.toml")
    v = config.placeholders(project, date=date, batch=a.batch, label=a.label, config=job.config, build_tag=a.build_tag,
                            source=source, overrides={})
    run_id = config.render(project.source.run_id, v)
    assert_run_id(run_id)
    v.update(run_id=run_id, tree_id=run_id, host=None, mount="", root=str(root))
    # The command runs as given; only the stage's steps, progress, budget, retry and tools come from edr.toml.
    spec = launch._spec(project, batch, job, [], [], v, stages=[replace(stage, cmd="", resume="")])
    spec["stages"][0]["cmd"] = shlex.join(argv)
    spec.pop("runtime", None)  # the tree of a tracked command is the caller's, as it is
    spec["collect"] = a.collect
    parsed = runid.parse_tag(source, project.source.nested)
    plan_ = launch.RunPlan(run_id=run_id, label=a.label, host=host, root=str(root), spec=spec, queued=False, source=source,
                           nested=parsed.nested if parsed else {})
    spec_path = project.state_dir / a.batch / f"{run_id}.spec.json"
    if spec_path.exists():
        raise Refuse(f"already tracked: {spec_path} exists")
    driver = sync.publish_driver(project.state_dir, launch.DRIVER_SRC, a.dry_run)
    launch.write_spec(project.state_dir, a.batch, plan_, driver, a.dry_run)
    if a.dry_run:
        c.emit(json.dumps(spec, indent=1), spec)
        return Exit.DONE
    now = int(time.time())
    c.db.upsert_batch({"batch": a.batch, "project": project.project, "source": source, "run_date": date})
    c.db.upsert_run({"run_id": run_id, "batch": a.batch, "label": a.label, "config": job.config, "build_tag": a.build_tag,
                     "source": source, "dirty": int("-dirty" in source), "host": host, "root": str(root), "created": now,
                     "phase": "setup", "state": "running", "started": now, "tree_id": run_id, "cores": stage.needs.cores,
                     "handle": str(Handle("track", f"{host}:{os.getpid()}", host))})
    c.db.add_event("user", run_id, "track", shlex.join(argv))
    print(f"{run_id}: {a.stage} on {host} {root}", file=sys.stderr)
    c.close()
    sys.stdout.flush()
    # The driver keeps this pid, so a signal to the job reaches it and its exit code is the job's.
    os.execv(sys.executable, [sys.executable, str(driver), str(spec_path)])
    return Exit.REFUSED  # not reached


def cmd_keep(c: Ctx, a: argparse.Namespace) -> int:
    """Write the keep file of a run."""
    row = c.resolve(a.handle)
    if not board.is_live(row):
        c.emit(f"{row['run_id']}: already {row['phase']}")
        return Exit.NOTHING
    c.emit(f"{row['run_id']}: " + _keep(c, row, a.hours, "user") + (" (dry)" if a.dry_run else ""))
    return Exit.DONE


def cmd_import(c: Ctx, a: argparse.Namespace) -> int:
    """Record a run tree that edr did not make, or its collected results, so `reuse`, `metrics` and `export` see it.

    The run holds what the flags say and nothing more, and a spec in the format of `launch`, so that a later extract
    reads every task."""
    assert_run_id(a.run_id)
    if not a.results and not (a.host and a.root):
        raise Refuse("import needs --host and --root, or --results DIR")
    _check_phase(c.project, a.phase)
    if a.started and a.ended and a.ended < a.started:
        raise Refuse("--ended is before --started")
    root = (a.root or "").rstrip("/")
    if root:
        if not root.startswith("/"):
            raise Refuse(f"'{root}' is not absolute")
        rc, _, _ = c.ssh.run(a.host, ["test", "-d", root])
        if rc != 0:
            raise Refuse(f"{a.host}:{root} is not a directory")
    results = _results_dir(c, a.run_id, a.results) if a.results else None
    if a.task_fields and not a.tasks:
        raise Refuse("--task-fields needs --tasks")
    given = config.load_tasks(Path(a.task_fields).expanduser(), c.project.site)[0] if a.task_fields else {}
    tasks = {t: given.get(t) or config.resolve_task(c.project, t) for t in a.tasks or []}
    params = dict(a.param or [])
    row = dict(run_id=a.run_id, batch=a.batch, label=a.label, config=a.config, build_tag=a.build_tag or "",
               source=a.source, dirty=int("-dirty" in a.source), host=a.host or "", root=root or None,
               created=int(time.time()), phase=a.phase, state="imported", stage="", step=-1,
               exit=0 if a.phase == "done" else None, started=a.started, updated=a.ended, counts={}, tree_id=a.run_id)
    spec = _import_spec(c.project, row, tasks, params)
    if (old := c.db.run(a.run_id)) and old.get("state") != "imported":
        raise Refuse(f"{a.run_id} exists in state {old.get('state')}; import records a new run or updates an imported one")
    batch = next((b for b in c.db.batches() if b["batch"] == a.batch), None)
    if batch and batch.get("source") and batch["source"] != a.source:
        raise Refuse(f"batch {a.batch} holds the source {batch['source']}; give {a.source} a batch of its own with --batch")
    if not runid.parse_tag(a.source, c.project.source.nested):
        form = "<hash>" + "-n<hash>" * len(c.project.source.nested)
        print(f"warning: {a.source} does not have the form of an edr checkout tag, {form} or {form}-dirty-<8 hex>")
    spec_path = c.project.state_dir / a.batch / f"{a.run_id}.spec.json"
    where = f"{a.host}:{root}" if root else f"results {results}"
    text = f"{where} as {a.label}@{a.batch}" + (f": {a.why}" if a.why else "")
    print(f"write {spec_path}" + (" (dry)" if a.dry_run else ""))
    if not a.dry_run:
        if batch is None:
            c.db.upsert_batch(dict(batch=a.batch, project=c.project.project, source=a.source))
        config.save_json(spec_path, spec)
        c.db.upsert_run(row)
        c.db.set_parameters(a.run_id, {**{k: row[k] for k in ("config", "build_tag", "source") if row[k]}, **params},
                            "import")
        c.db.set_task_fields(a.run_id, [(t, k, v, "import" if t in given else "resolver")
                                        for t, task in tasks.items() for k, v in task.fields.items()])
        if results:
            rows = _import_results(c, row, results, spec)
            failed = metrics.failures(rows)
            text += f", {sum(r['value'] is not None for r in rows)} metrics" + (
                f"; {metrics.failure_text(failed)}" if failed else "")
            # A task counts done when its files gave a value and no failed row.
            got = c.db.metrics(run_ids=[a.run_id])
            parsed = {m["task"] for m in got if m["value"] is not None} - {m["task"] for m in got if m["value"] is None}
            done = len(parsed & set(tasks))
            row["counts"] = {"done": done, "failed": len(tasks) - done}
            c.db.upsert_run({"run_id": a.run_id, "counts": row["counts"]})
        c.db.add_event("user", a.run_id, "import", text)
    c.emit(f"imported {text}" + (" (dry)" if a.dry_run else ""), row)
    return Exit.DONE


def _results_dir(c: Ctx, run_id: str, text: str) -> Path:
    """The collected results to link as data/results/<run_id>; a different tree there is refused."""
    src = Path(text).expanduser().resolve()
    if not src.is_dir():
        raise Refuse(f"'{text}' is not a directory")
    dest = c.project.data / "results" / run_id
    if (dest.is_symlink() or dest.exists()) and dest.resolve() != src:
        raise Refuse(f"{dest} exists and is not {src}")
    return src


def _import_spec(project: Project, row: Row, tasks: dict, params: dict[str, str]) -> dict[str, Any]:
    """The spec of an imported run in the format of `launch`: the run, its vars from the `vars.<name>` parameters,
    and each task group with the tasks, their directories and their fields. It renders no command, since edr ran
    none."""
    job_vars = {k[5:]: v for k, v in params.items() if k.startswith("vars.")}
    values = config.placeholders(project, **{k: v for k, v in row.items() if isinstance(v, (str, int, float))}, vars=job_vars)
    groups = [{"name": name, "tasks": [
        {"id": t.id, "dir": os.path.normpath(os.path.join(
            row["root"] or "", config.render(st.task_dir, {**values, **{f"task.{k}": v for k, v in t.fields.items()}}))),
         "fields": t.fields}
        for t in tasks.values()]} for name, st in project.stages.items() if st.is_group and tasks]
    return {"schema": 1, "project": project.project, **{k: row[k] for k in ("run_id", "batch", "label", "config")},
            "vars": job_vars, "host": row["host"], "root": row["root"], "stages": groups}


def _import_results(c: Ctx, row: Row, src: Path, spec: dict) -> list[Row]:
    """Link `src` under data/results, extract every metric and parameter of the project from it and check the run, as
    `edr extract` does, and return the metric rows that went in."""
    dest = c.project.data / "results" / row["run_id"]
    if not (dest.is_symlink() or dest.exists()):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(src)
    rows = watch.extract_run(c.project, c.db, row, {}, spec, actor=None)
    with c.db.conn:  # one commit for the rows of the run
        new = [r for r in rows if c.db.add_metric(r)]
        watch.check_run(c.project, c.db, row, {}, spec)
        return new


def cmd_export(c: Ctx, a: argparse.Namespace) -> int:
    """Write a frozen snapshot of one or more sources, or the project database into an MLflow store."""
    if a.mlflow:
        if a.dry_run:
            n = len([r for r in c.db.runs() if not a.source or r.get("source") in a.source])
            c.emit(f"{a.mlflow}: {n} runs (dry)", {"runs": n})
            return Exit.DONE
        from .mlflow_export import export_mlflow

        res = export_mlflow(c.project, c.db, Path(a.mlflow), a.source)
        c.db.add_event("user", "", "export", f"mlflow {' '.join(a.source or ['every source'])} -> {a.mlflow}")
        c.emit(f"{res['tracking_uri']}: {len(res['written'])} runs written, {len(res['skipped'])} already there", res)
        return Exit.DONE
    if not a.source or not a.out:
        raise Refuse("export needs --source and --out, or --mlflow DIR")
    labels = a.labels.split(",") if a.labels else None
    manifest = export.export(c.project, c.db, a.source, Path(a.out), labels, a.dry_run, a.files, a.globs)
    if not a.dry_run:
        c.db.add_event("user", "", "export", f"{' '.join(a.source)} -> {os.path.abspath(a.out)}")
    missing = len(manifest["missing_files"])
    c.emit(f"{a.out}: {len(manifest['runs'])} runs ({len(manifest['incomplete'])} not done), {len(manifest['skipped'])} "
           f"skipped, {len(manifest['files'])} files" + (f", {missing} cited files missing" if missing else "")
           + (" (dry)" if a.dry_run else ""), manifest)
    return Exit.DONE


def cmd_coverage(c: Ctx, a: argparse.Namespace) -> int:
    """For each row of a demand list, whether the `pick` of a label at the row's source holds a value at its stage and
    task; else whether that run is running or failed, or a run at another source holds it."""
    try:
        with open(a.demand, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, skipinitialspace=True)
            demand = list(reader)
    except OSError as e:
        raise Refuse(f"{a.demand}: {e.strerror}") from None
    head = reader.fieldnames or []
    keys = [k for k in ("label", "build_tag") if k in head]
    if not keys or not {"stage", "task"} <= set(head):
        raise Refuse(f"{a.demand}: the header needs label or build_tag, stage and task")
    bad = [n for n, d in enumerate(demand, 2) if not d["stage"] or not any(d[k] for k in keys)]
    if bad or not demand:
        raise Refuse(f"{a.demand}: " + (f"line {bad[0]} needs a stage, and a label or a build_tag" if bad else "no row"))
    groups: dict[tuple[str, str], list[Row]] = {}
    for r in c.db.runs():
        groups.setdefault((r["label"], r["source"]), []).append(r)
    picks = [pick(g) for g in groups.values()]
    held = {(m["run_id"], m["stage"], m["task"]) for stage in {d["stage"] for d in demand}
            for m in c.db.metrics(stage=stage) if m["value"] is not None}
    out = []
    for d in demand:
        task, source = d["task"] or "", d.get("source") or ""
        mine = [p for p in picks if all(has_label(p, d[k], c.project.stages) if k == "label" else p.get(k) == d[k]
                                        for k in keys if d[k])]
        here = [p for p in mine if p["source"] == source or not source]
        has = [p for p in mine if (p["run_id"], d["stage"], task) in held]
        status, runs = next(((s, rs) for s, rs in (
            ("held", [p for p in has if p in here]), ("running", [p for p in here if board.is_live(p)]),
            ("failed", [p for p in here if p.get("phase") != "done"]), ("elsewhere", has)) if rs), ("missing", []))
        out.append({**{k: d[k] or "" for k in keys}, "stage": d["stage"], "task": task, "source": source,
                    "status": status, "runs": [{k: p.get(k) for k in ("run_id", "label", "source", "phase")} for p in runs]})
    cols = [*keys, "stage", "task", "source", "status"]
    body = [[*(r[k] for k in cols), ", ".join(f"{p['label']}@{p['source']}" + ("" if p["phase"] == "done" else f" {str(p['phase'])[:40]}")
                                              for p in r["runs"])] for r in out]
    gaps = sum(r["status"] != "held" for r in out)
    c.emit(Group(board.table([*cols, "runs"], body), Text(f"{len(out) - gaps} of {len(out)} rows held")), out)
    return Exit.REFUSED if gaps else Exit.DONE


def cmd_stop(c: Ctx, a: argparse.Namespace) -> int:
    """Stop one run through the driver, or the stop file with --after-task."""
    row = c.target(a.handle)
    hb = c.heartbeat(row)
    if row.get("state") == "queued":
        # The watcher launches every row in state queued; a stopped row never starts.
        c.emit(f"{row['run_id']}: queued, marked stopped" + (" (dry)" if a.dry_run else ""),
               {"run_id": row["run_id"], "stopped": True})
        if not a.dry_run:
            c.db.upsert_run({"run_id": row["run_id"], "state": "stopped"})
            c.db.add_event("user", row["run_id"], "stop", f"queued: {a.why}")
        return Exit.DONE
    if not board.is_live(hb or row):
        c.emit(f"{row['run_id']}: already {(hb or row).get('phase')}")
        return Exit.NOTHING
    # The driver's own handler ends the run in seconds; limits.grace_s is the watcher's delay.
    ok = launch.stop(c.ssh, c.db, row, hb, after_task=a.after_task, now=a.now, grace_s=30 if a.now else 60,
                     dry_run=a.dry_run, why=a.why, state=c.project.state_dir, backend=c.backend)
    c.data = {"run_id": row["run_id"], "stopped": ok}
    if not ok and not a.now:
        print(f"{row['run_id']}: still alive; use --now")
    return Exit.DONE if ok else Exit.HOSTS


def cmd_retire(c: Ctx, a: argparse.Namespace) -> int:
    """Remove the run tree, or the prune targets, after the guard; --batch marks RETIRED."""
    if sum(map(bool, (a.handle, a.batch, a.host))) != 1:
        raise Refuse("retire needs one of a handle, --batch and --host")
    if a.host and not a.prune:
        raise Refuse("--host takes --prune")
    project, dry = c.project, " (dry)" if a.dry_run else ""
    if a.host:
        rows = [r for r in c.db.runs() if r.get("host") == a.host and r.get("root") and not board.is_live(r)
                and r.get("state") != "retired"]
    else:
        rows = c.db.runs(batch=a.batch) if a.batch else [c.target(a.handle)]
    # The stages a tree has left need its files: --host skips such a run, and a named run is pruned all the same.
    left = {r["run_id"]: launch.stages_left(project, c.db, r)[0] for r in rows} if a.prune else {}
    for r in rows if a.host else []:
        if left[r["run_id"]]:
            print(f"{r['run_id']}: skipped, stages left on its tree: {', '.join(left[r['run_id']])}")
    rows = [r for r in rows if not (a.host and left[r["run_id"]])]
    if a.host and not rows:
        c.emit(f"no finished run with a tree on {a.host} and no stage left")
        return Exit.NOTHING
    if a.batch and not rows and not (project.state_dir / a.batch).is_dir():
        c.emit(f"no runs in batch {a.batch}")
        return Exit.NOTHING
    checked, retiring = [], {r["run_id"] for r in rows}
    for row in rows:
        run_id, hb = row["run_id"], c.heartbeat(row)
        host, root = row.get("host") or hb.get("host"), row.get("root") or hb.get("root")
        targets = _retire_targets(c, row, hb, str(root), a) if root else []
        pid, handle = hb.get("driver_pid"), run_handle(c.backend.name, row, hb)
        if root and board.is_live(hb) and pid and handle and not gone(c.backend, handle):
            raise Refuse(f"{run_id}: driver {pid} is alive on {host}; stop it first")
        # A driver writes its first heartbeat within seconds; none after dead_s means it never came up.
        if root and not hb and board.is_live(row) and time.time() - (row.get("started") or 0) < project.limits.dead_s:
            raise Refuse(f"{run_id}: no heartbeat yet; wait for the driver, then stop it first")
        if root:
            _refuse_shared_root(c, row, str(host), str(root), retiring, bool(a.prune))
        checked.append((row, hb, host, root, targets))
    worktree = _worktree_target(c, a.batch) if a.batch and not a.prune else None
    # The archive step: every named list is on the head node before any rm runs.
    for row, hb, host, root, targets in checked:
        for name in (a.collect.split(",") if a.collect and root else []):
            res = collect.collect_on_request(project, c.ssh, c.db, row, name, a.dry_run)
            print(f"{row['run_id']}: collect {name}: {res.files} files{dry}")
            if res.failures:
                raise Refuse(f"{row['run_id']}: collect {name}: {res.failures[0]}; nothing removed")
            if not a.dry_run:
                c.db.add_event("user", row["run_id"], "collect", f"{name}: {res.files} files")
    failed, done = 0, []
    for row, hb, host, root, targets in checked:
        run_id = row["run_id"]
        if left.get(run_id) and targets:
            print(f"{run_id}: stages left on its tree: {', '.join(left[run_id])}; they may need what the prune removes")
        for t in targets:
            print(f"{run_id}: rm -rf {t} on {host}{dry}")
            if a.dry_run:
                continue
            rc, _, err = c.ssh.run(str(host), f"rm -rf -- {shlex.quote(t)}")
            if rc != 0:
                failed += 1
                print(f"{run_id}: rm rc {rc}: {err.strip()}", file=sys.stderr)
        if a.dry_run or (a.prune and not root):
            continue
        if not a.prune and board.is_live(hb or row):
            phase = f"ABANDONED:{a.why}"
            if hb:
                hb["phase"], hb["exit"] = phase, 1 if hb.get("exit") is None else hb["exit"]
                config.save_json(project.state_dir / str(row["batch"]) / f"{run_id}.json", hb)
            c.db.upsert_run({"run_id": run_id, "phase": phase, "exit": 1, "state": "retired"})
        elif not a.prune:
            c.db.upsert_run({"run_id": run_id, "state": "retired"})
        c.db.add_event("user", run_id, "prune" if a.prune else "retire", f"{a.why}: " + (" ".join(targets) or "no tree"))
        done.append(run_id)
    if a.batch and not a.prune and not a.dry_run:
        (project.state_dir / a.batch).mkdir(parents=True, exist_ok=True)
        (project.state_dir / a.batch / "RETIRED").touch()
        c.db.mark_batch_retired(a.batch)
    if worktree is not None:
        print(f"{a.batch}: rm -rf {worktree}{dry}")
        if not a.dry_run:
            try:
                shutil.rmtree(worktree)
                c.db.add_event("user", "", "retire", f"{a.why}: worktree {worktree}")
            except OSError as e:
                failed += 1
                print(f"{a.batch}: worktree not removed: {e}", file=sys.stderr)
    c.data = {"retired": done, "failed": failed}
    return Exit.HOSTS if failed else Exit.DONE


def _worktree_target(c: Ctx, batch: str) -> Path | None:
    """The checked-out tree of the batch's source, when no other batch that is not retired has it.

    A tree that fails the guard is kept with one line on stdout, so the run trees still go.
    """
    rows = {b["batch"]: b for b in c.db.batches()}
    source = str((rows.get(batch) or {}).get("source") or "")
    if not source or any(b["batch"] != batch and not b.get("retired") and b.get("source") == source for b in rows.values()):
        return None
    path = c.project.source.worktrees / source
    if not path.is_dir():
        return None
    if path.resolve() == c.project.source.repo.resolve():
        raise Refuse(f"{path} is the source repository")
    try:
        return assert_safe_target(path, c.project.safety.marker, c.project.safety.min_depth)
    except Refuse as e:
        print(f"{batch}: worktree kept: {e}; remove it with rm -rf")
        return None


def _refuse_shared_root(c: Ctx, row: Row, host: str, root: str, retiring: set[str], prune: bool) -> None:
    """A tree that another run still uses is never a delete target of this call.

    Every power run on a reused tree has that tree as its root. Removing it for one
    run would take the netlist of the others, and of a live driver among them.
    """
    others = [r for r in c.db.runs() if r["run_id"] not in retiring and r.get("root") == root
              and (r.get("host") or "") == host and r.get("state") != "retired"]
    live = [r["run_id"] for r in others if board.is_live(c.heartbeat(r) or r)]
    if live:
        raise Refuse(f"{row['run_id']}: root {root} is in use by a live run ({', '.join(live[:3])}); "
                     "stop it first")
    if prune:
        return
    results = c.project.data / "results"
    uncollected = [r["run_id"] for r in others if not (results / r["run_id"] / "log").is_dir()]
    if uncollected:
        raise Refuse(f"{row['run_id']}: root {root} is shared with a run whose results are not collected "
                     f"({', '.join(uncollected[:3])}); run edr watch --once, or retire them together with --batch")


def _retire_targets(c: Ctx, row: Row, hb: dict, root: str, a: argparse.Namespace) -> list[str]:
    project = c.project
    if a.prune:
        scalars = {k: v for k, v in {**hb, **row}.items() if isinstance(v, (str, int, float))}
        values = config.placeholders(project, **scalars)
        names = a.prune.split(",")
        if missing := [n for n in names if not any(n in st.prune for st in project.stages.values())]:
            raise Refuse(f"no stage has prune.{missing[0]}")
        targets = [posixpath.join(root, config.render(p, values))
                   for n in names for st in project.stages.values() for p in st.prune.get(n, [])]
    else:
        if not (project.data / "results" / row["run_id"] / "log").is_dir() and not a.uncollected:
            raise Refuse(f"{row['run_id']}: results not collected; run edr watch --once, or pass --uncollected")
        targets = [root]
    return [str(assert_safe_target(t, project.safety.marker, project.safety.min_depth)) for t in targets]


def cmd_watch(c: Ctx, a: argparse.Namespace) -> int:
    """The watcher: one cycle, a check, or the loop with the bot."""
    project = c.project
    if a.dry_run:
        watch.cycle(project, c.ssh, c.db, [], dry_run=True, backend=c.backend)
        return Exit.DONE
    if a.check:
        return watch.check(project, _notifiers(c))
    path = project.state_dir / "watch.lock"
    fd = home.lock(path)
    if fd is None:
        print(f"edr watch: pid {home.holder(path)} watches {project.project} already")
        return Exit.NOTHING
    try:
        return watch.run_forever(project, c.ssh, c.db, _notifiers(c, a.served), once=a.once, served=a.served)
    finally:
        os.close(fd)


def cmd_serve(c: Ctx, a: argparse.Namespace) -> int:
    """The supervisor: one watcher per registered project, and the work of the user."""
    if a.unit:
        cmd, exe, what = serve.pinned()
        if cmd:
            print(("dry: " if a.dry_run else "") + shlex.join(cmd), file=sys.stderr)
            if not a.dry_run and subprocess.run(cmd, stdout=sys.stderr).returncode != 0:
                raise Refuse(f"{shlex.join(cmd)} failed")
        c.emit(serve.unit(exe, what), {"install": cmd, "edr": exe})
        return Exit.DONE
    if a.dry_run:
        return _serve_plan(c)
    notifiers = serve.notifiers(None if a.check or a.once else Router(a))
    if a.check:
        return serve.check(notifiers)
    return serve.run(notifiers, once=a.once)


def _serve_plan(c: Ctx) -> int:
    """What edr serve would watch: every registered project, whether it loads, and who watches it now."""
    rows = []
    for name, path in sorted((p.name, home.owner(p.name)) for p in (home.root() / "projects").glob("*")):
        if path is None:
            continue
        try:
            project = config.load_project(path)
        except ConfigError as e:
            rows.append({"project": name, "root": str(path), "action": f"no watcher until it loads: {e}"})
            continue
        pid = home.holder(project.state_dir / "watch.lock")
        rows.append({"project": name, "root": str(path),
                     "action": f"none, pid {pid} watches it" if pid else "start edr watch --served"})
    pid = home.holder(home.root() / "serve.lock")
    head = f"pid {pid} serves now. " if pid else ""
    body = [[r["project"], r["root"], r["action"]] for r in rows]
    c.emit(Group(Text(head + "A supervisor would do this (dry):"), board.table(["project", "directory", "watcher"], body))
           if rows else head + "No project is registered (dry).", rows)
    return Exit.DONE if rows else Exit.NOTHING


def cmd_register(c: Ctx, a: argparse.Namespace) -> int:
    """Link the project into the registry of the user root."""
    p = c.project
    new = home.register(p.project, p.root, a.dry_run)
    c.emit(f"{'link' if new else 'already linked:'} {home.link(p.project)} -> {p.root}" + (" (dry)" if a.dry_run else ""),
           {"project": p.project, "root": str(p.root), "new": new})
    return Exit.DONE if new else Exit.NOTHING


def cmd_unregister(c: Ctx, a: argparse.Namespace) -> int:
    """Remove the link of the project from the registry."""
    p = c.project
    gone = home.unregister(p.project, p.root, a.dry_run)
    c.emit(f"remove {home.link(p.project)}" + (" (dry)" if a.dry_run else "") if gone else f"{p.project} is not registered",
           {"project": p.project, "removed": gone})
    return Exit.DONE if gone else Exit.NOTHING


def cmd_notify(c: Ctx, a: argparse.Namespace) -> int:
    """Send one message, the board or the digest through every configured notifier."""
    if (a.text is not None) + a.board + a.digest != 1:
        raise Refuse("give TEXT, --board or --digest, one of them")
    if a.board:
        title, html = "board", Actions(c).status_text()
    elif a.digest:
        title, html = "digest", _digest({**census.projects(), c.project.project: c.project})
    else:
        title, html = "note", tgfmt.esc(a.text)
    if a.dry_run:
        c.emit(tgfmt.head(c.project.project, title) + "\n" + html + "\n(dry)", {"sent": 0, "text": untag(html)})
        return Exit.DONE
    notifiers = make_notifiers(config.load_user(), c.project.site, c.project, c.db)
    if not notifiers:
        raise Refuse(f"no notifier is configured in {config.DEFAULT_USER}; see docs/guides/alerts.md")
    sent = sum(n.post(title, html, a.silent) for n in notifiers)
    c.emit(f"sent to {sent} of {len(notifiers)} notifiers", {"sent": sent, "text": untag(html)})
    return Exit.DONE if sent == len(notifiers) else Exit.REFUSED


def _notifiers(c: Ctx, served: bool = False) -> list:
    """The channels of the project; unless it is served, the bot polls once the watcher holds serve.lock."""
    project = c.project
    # The bot polls in its own thread.
    return make_notifiers(config.load_user(), project.site, project, Database(project.data / "edr.db", threads=True),
                          None if served else Router(c.a))


def _digest(found: dict[str, Project]) -> str:
    """The digest of `found` since the last daily one, as Telegram HTML; it moves nothing."""
    now = time.time()
    return digest.text(found, digest.since(home.Store().get_store("digest", {}), now), now)


class Router:
    """The projects of the bot: the Actions of each, the handle search over all, and the texts of all at once."""

    def __init__(self, a: argparse.Namespace) -> None:
        self.a = argparse.Namespace(**{**vars(a), "json": False, "dry_run": False, "command": "status"})

    def names(self) -> list[str]:
        """The registered projects that load."""
        return sorted(census.projects())

    @contextlib.contextmanager
    def actions(self, name: str) -> Iterator[Actions]:
        """The Actions of project `name`, with its database open for the block."""
        project = census.projects().get(name)
        if project is None:
            raise Refuse(f"no registered project {name}; the projects: {', '.join(self.names()) or 'none'}")
        c = Ctx(self.a)
        c._project = project
        c._db = Database(project.data / "edr.db")
        try:
            yield Actions(c)
        finally:
            c.close()

    def pick(self, args: list[str], here: str | None) -> tuple[str, list[str]]:
        """The project a command names in its first argument, else `here`, the project of the alert it replies to or
        of its topic, else the only one; and the arguments that remain. Refuse lists the projects when none of these
        gives one."""
        names = self.names()
        if args and args[0] in names:
            return args[0], args[1:]
        if here:
            return here, args
        if len(names) == 1:
            return names[0], args
        raise Refuse("name a project first: " + ", ".join(names) if names else "no registered project")

    def resolve(self, handle: str) -> tuple[str, str]:
        """(project, handle) of one run: `project/label@batch`, `#n` of the last board, or a handle that one project knows."""
        if "/" in handle:
            project, _, h = handle.partition("/")
            return project, h
        if handle.startswith("#"):
            board_ = home.Store().get_store("last_board", [])
            n = int(handle[1:]) if handle[1:].isdigit() else 0
            if not 1 <= n <= len(board_):
                raise Refuse(f"{handle}: the last board has {len(board_)} runs")
            return board_[n - 1][0], board_[n - 1][1]
        found = []
        for name in self.names():
            with self.actions(name) as act:
                try:
                    found.append((name, board.handle(act.c.resolve(handle), act.c.db.runs())))
                except Refuse:
                    pass
        if len(found) != 1:
            raise Refuse(f"{handle}: " + ("no run of any project has it" if not found else
                                          "more than one project has it: " + ", ".join(f"{p}/{h}" for p, h in found)))
        return found[0]

    def run_of(self, project: str, msg_id: int) -> str | None:
        """The run id of the alert `msg_id` of project `project`, while it is younger than REPLY_DAYS."""
        with self.actions(project) as act:
            hit = act.c.db.get_store("telegram", {}).get("replies", {}).get(str(msg_id))
        return hit[0] if hit and time.time() - hit[1] < REPLY_DAYS * 86400 else None

    def replied(self, msg_id: int) -> tuple[str, str] | None:
        """(project, run id) of the alert `msg_id` of any project."""
        return next(((n, r) for n in self.names() if (r := self.run_of(n, msg_id))), None)

    def topic_of(self, thread: int) -> str | None:
        """The project whose forum topic is `thread`."""
        for name in self.names():
            with self.actions(name) as act:
                if act.c.db.get_store("telegram", {}).get("topic") == thread:
                    return name
        return None

    def board_text(self) -> str:
        """The board of every project, the one the supervisor pins."""
        now = time.time()
        return serve.global_board(census.projects(), census.read(float("inf"), now), now)

    def projects_text(self) -> str:
        """One line per project: its watcher and its live runs."""
        c = Ctx(self.a)
        cmd_projects(c, self.a)
        return tgfmt.projects(c.data or [])

    def events_text(self, n: int) -> str:
        """The last `n` events of every project, newest first, each run as project/handle."""
        rows, names = [], {}
        for name in self.names():
            with self.actions(name) as act:
                rows += act.c.db.events(n=n)
                names.update({i: f"{name}/{h}" for i, h in board.handles(act.c.db.runs()).items()})
        return tgfmt.events(sorted(rows, key=lambda e: e["ts"])[-n:], names)

    def hosts_text(self) -> str:
        """The free room of every host of every project and your runs on it."""
        found = census.projects()
        sites = census.host_sites(found)
        probes = {h: p for site in {id(s): s for s in sites.values()}.values()
                  for h, p in probe_all(Ssh(site), [h for h in site.hosts if sites.get(h) is site]).items()}
        rows = census.host_view(probes, census.live_runs(found.values(), time.time()),
                                {h: floor(sites[h], h) for h in probes}, Placement())
        return tgfmt.hosts(rows)

    def tools_text(self) -> str:
        """Every tool of the sites of every project, once."""
        rows: dict[str, Row] = {}
        for name in self.names():
            with self.actions(name) as act:
                for r in _tool_rows(act.c):
                    rows.setdefault(r["tool"], r)
        return tgfmt.tools(rows.values())

    def digest_text(self) -> str:
        """The digest of every project."""
        return _digest(census.projects())


# --- parser and main

class Exit(IntEnum):
    """The exit codes of every command. `EXIT` gives the meaning of each; `EXITS` the refinements per command."""

    DONE = 0
    REFUSED = 1
    NOTHING = 2
    HOSTS = 3
    INTERRUPTED = 130


EXIT = {Exit.DONE: "done",
        Exit.REFUSED: "a guard refused, a config file is wrong, or the input is bad",
        Exit.NOTHING: "nothing to do",
        Exit.HOSTS: "a host did not answer, or a host command failed",
        Exit.INTERRUPTED: "interrupted"}
EXITS: dict[str, dict[Exit | int, str]] = {}  # command -> the codes it refines; `_parser` fills it
HANDLE = "label@batch, label@source, a run id prefix, or #n from the last board"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # Bad input exits 1 like a refused guard, not the 2 of argparse.
        self.print_usage(sys.stderr)
        print(f"edr: {message}", file=sys.stderr)
        raise SystemExit(Exit.REFUSED)


def _d(text: str) -> str:
    return textwrap.dedent(text).strip()


def _read_commands() -> str:
    """The epilog paragraph on the commands that never create the database, wrapped like the rest."""
    names = ", ".join(sorted(_READ_COMMANDS))
    return textwrap.fill(f"These commands never create data/edr.db: {names}. Without the file they read an empty "
                         "database in memory.", 84, initial_indent=" " * 8, subsequent_indent=" " * 8).lstrip()


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="edr", description="Run your flow on the hosts, watch the runs, keep them in one project database "
                                "and export snapshots.",
                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=_d(f"""
        edr finds edr.toml in the current directory or a parent, so it works from
        anywhere below the project. Without one it refuses. -P NAME, or the
        variable EDR_PROJECT, picks a registered project from any directory
        instead; EDR_PROJECT is refused inside the directory of another project.
        The registry is ~/.edr/projects/, or projects/ under EDR_HOME: one link
        per project name to its directory. launch, continue, track, import and
        watch write the link, and a name that belongs to another directory
        stops them.

        --json, before the command as in edr --json status or after it, prints
        one object instead of the text:
        {{"code": 0, "data": {{}}, "output": "the text a person would see"}}.
        code is the exit code, data the command's result as structured data, and
        output the text.

        --dry-run exists on every command that writes. A dry run prints every path
        and every command with the mark (dry) or the prefix dry: and writes
        nothing: no date pin, no spec, no file on a host, no database row, no
        event, not even an empty database.

        --why <text> is required on stop and retire, and optional on import. The
        text goes into the events table together with the actor.

        {_read_commands()}

        A table on a terminal has colour: a run is green while it runs, cyan when
        queued, yellow when stale, red when dead, hung, over budget, an orphan or
        failed, and dim when done or retired. A pipe or the NO_COLOR variable
        gets the same text with no escape code, and --json never carries any.

        A handle names one run in one of four forms. label@batch is the one run
        with that label in that batch; when the batch holds several, the handle
        is refused and the error lists them. label@source is the run of record
        of that label at that source tag: the newest run by start time that
        ended done, else the newest run. A name after the @ that is both a batch
        and a source is refused. A run id prefix is the one run whose id starts
        with it; an ambiguous prefix is refused. The form #n is row n of the
        last board that edr status printed. stop, retire and continue first
        print the run id and the phase of the run they act on to stderr, also
        with --dry-run.

        plan and launch take the batch as an argument, which defaults to
        EDR_BATCH and then to the newest batch directory in the state. status
        --batch defaults to EDR_BATCH and then to every batch. retire needs a
        handle, --batch or --host.
        """))
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("-P", "--project", metavar="NAME", help="the registered project NAME, from any directory")
    p.add_argument("--version", action="version", version=f"edr {__version__}", help="print the version and exit")
    p.set_defaults(dry_run=False)
    sub = p.add_subparsers(dest="command", metavar="command", required=True)

    def command(name: str, help_: str, description: str = "", write: bool = False, why: bool = False,
             exits: dict[Exit | int, str] | None = None) -> argparse.ArgumentParser:
        EXITS[name] = exits or {}
        codes = {Exit.DONE: EXIT[Exit.DONE], Exit.REFUSED: EXIT[Exit.REFUSED], **EXITS[name]}
        s = sub.add_parser(name, help=help_, description=_d(description) or help_,
                           epilog="exit codes: " + "; ".join(f"{int(c)} {t}" for c, t in sorted(codes.items())),
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        s.set_defaults(fn=globals()["cmd_" + name])
        if write:
            s.add_argument("--dry-run", action="store_true", help="print what would happen and write nothing")
        if why:
            s.add_argument("--why", required=True, help="the reason; it goes into the events table")
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=f"the same as edr --json {name}")
        return s

    s = command("brief", "what a session reads first: the project, its flow, site and state", """
        Prints a Markdown briefing for a person or an agent who has not seen
        the project before. It says what the project is: its name, root, source
        repository, the sources checked out under the worktrees directory and
        the backend. It lists the stages in order with what each one collects
        and the tools it needs, then the hosts with the marks of their last
        probe from the host_samples table and the tools with the seats their
        probe reports. The state follows: the runs per batch and state, the
        live runs, every run the triage proposes a command for with that
        command, the runs that the checks flag, and the last ten events. Each
        batch names the source tags
        of its runs and how many commits each one lags behind [source] ref,
        counted from the commit of the tree without the nested and dirty
        parts of the tag. It ends with the
        project's CLAUDE.md and AGENTS.md, when they exist, and the
        documentation.

        With --run, it prints the history of one run instead: its identity,
        its flags, its stage and step times from stage_runs and step_runs, its events with
        their reasons, the last 20 lines of its current stage log read from the host,
        the last value of each metric, and the command the triage proposes
        with the reason. --json gives either as an object.

        A Claude Code SessionStart hook that runs edr brief starts every
        session with the briefing; docs/guides/agents.md shows the hook.
        """)
    s.add_argument("--run", metavar="HANDLE", help="the history of one run: " + HANDLE)
    s = command("status", "the board, or one run", """
        Without a handle, status prints the board: one line per run of every
        batch that is not retired, live runs first and dead ones on top. The columns are the row
        number, label, source tag, host, state, phase (its first 40 characters),
        stage/step, heartbeat age, failed and done task counts, and core-h: the
        hours so far times the cores the run reserved, the most that any of its
        stages needs. A live stage with steps shows <stage>, starting until its
        first step. The state of a live run
        follows the heartbeat age (running, stale, dead) or the watcher's last
        verdict (hung, host_full, ...). A finished run shows its phase class:
        done, incomplete, failed, over_budget, stopped or killed.

        With a handle, it prints one run: its identity, state and disk, the
        flags of its checks, every stage and task row, the CPU, RSS, tree size
        and free disk the driver sampled over the
        run, the metrics, and the log tail from the heartbeat. A finished run
        shows driver exit <n> (<phase>): the code of the driver, whose phase
        names the stage that failed. The command exit column of the stage table
        is the code of the stage command itself. The tasks line with the done
        and failed counts appears only for a run with a task group.

        --all prints the board of every registered project in one table, with
        the project in the first column; it needs no project directory.

        --metric NAME (repeatable) adds a column per metric to the board: the
        value of each run at its step of record, else at its own last step,
        with FAIL when it breaks the pass rule, and the stage and step. A run
        that lacks its step of record shows missing, and a line under the
        board names the steps it has. --source (repeatable) keeps the runs of
        those source tags. --csv writes the board as CSV: run_id, label,
        batch, source, host, state and phase, then per metric its value,
        stage, step and verdict; the missing lines go to stderr. --json
        gives each metric as a row of compare --json, with the value, stage,
        step, source file and verdict of each run by run id, and the missing
        runs.
        """, exits={Exit.HOSTS: "with --live, a host did not answer"})
    s.add_argument("handle", nargs="?", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="one batch; default EDR_BATCH, else every batch")
    s.add_argument("--source", action="append", metavar="SOURCE", help="the runs of the exact source tag; repeatable")
    s.add_argument("--metric", action="append", metavar="NAME",
                   help="a column with this metric at the step of record, by name or canonical name; repeatable")
    s.add_argument("--csv", action="store_true", help="the board as CSV on stdout")
    s.add_argument("--narrow", action="store_true", help="48 columns, two lines per live run, for an ssh app on a phone")
    s.add_argument("--watch", action="store_true", help="redraw every heartbeat_s seconds; Ctrl-C ends it")
    s.add_argument("--live", action="store_true", help="ask each host whether the driver exists; a gone driver shows dead")
    s.add_argument("--triage", action="store_true", help="every run not running, with one proposed command")
    s.add_argument("--digest", action="store_true", help="the daily digest of this project, or with --all of every "
                                                         "project, as plain text")
    s.add_argument("--all", action="store_true", help="the runs of every registered project, with a project column")
    s = command("events", "the last events", """
        Prints the last N events, oldest first, with the time, the actor (user,
        watch or telegram), the run, the kind and the text.
        """, exits={Exit.NOTHING: "no event"})
    s.add_argument("--since", metavar="T", help="only events newer than this: 30m, 2h, 1d or seconds")
    s.add_argument("--run", metavar="HANDLE", help="the events of one run")
    s.add_argument("-n", type=int, default=50, help="the last N events; default 50")
    s = command("hosts", "probe every host: its free room, and your runs on it", """
        Probes every host of the site file, all at once, and prints one row per
        host: whether a run can start there; the free cores, RAM and scratch
        of total, where the scratch is the largest writable one of the host's
        list; the idle GPUs of total, where idle means under 5 % utilisation
        and under 5 % memory in use; the processes that match tool_procs,
        yours and other users'; and your live runs on the host, by project over
        every registered project, with the cores, RAM and scratch they use,
        read from their heartbeats.

        The mark says whether a run can start: 🟢 when the host passes every
        rule of [placement] of this project and keeps its scratch above the
        floor host_free_min_gb, 🔴 when it does not, and ⚫ when the host did
        not answer. The hosts where a run can start come first, the most free
        cores first. Under the table, a line per host says why no run can
        start there, and when your own runs fill the host: your trees push its
        scratch under the floor, or your runs use most of its busy cores or
        RAM. That is the case where your runs block other people's work.

        Outside a project, hosts reads the default site file
        ~/.config/edarunner/site.toml and the default [placement]. --json
        gives each row with the probe numbers, runs, our_cores, our_ram_gb,
        our_gb, floor_gb, start, why and note.

        --history probes nothing. It reads the host_samples table, where the
        watcher keeps one probe per host and cycle for 30 days, and prints one
        row per host: the first and last sample, and a line each of cores in
        use (the load, capped at the cores), RAM, scratch and busy GPUs over
        --since, each with its peak and its last value.

        --batch B takes the window of a batch instead of --since: from the
        start of its first run to the last heartbeat of its last run, or to now
        while a run of it lives. It shows the hosts the batch ran on, and under
        each one a line of the batch's own use from the run_samples table: the
        CPU of its runs in cores, their RSS and the size of their trees, summed
        at each host sample.
        """, exits={Exit.HOSTS: "a host did not answer", Exit.NOTHING: "with --history, no sample"})
    s.add_argument("--history", action="store_true",
                   help="no probe: the samples the watcher kept, one line per host over --since")
    s.add_argument("--since", default="1d", metavar="T", help="with --history: 30m, 2h, 1d or seconds; default 1d")
    s.add_argument("--batch", metavar="B",
                   help="the history over the window of this batch, with a line of its own use under each of its hosts")
    s.add_argument("--narrow", action="store_true",
                   help="only the mark (column ok), host, free cores, free scratch and your runs, in 48 columns")
    command("projects", "every registered project, its watcher and its live runs", """
        Prints one row per project of the registry ~/.edr/projects/: its
        directory, the pid of the watcher that holds its watch.lock, its live
        runs, and a note when its files do not load or the link names no
        project. It needs no project directory.
        """, exits={Exit.NOTHING: "no project is registered"})
    command("tools", "every site tool: free seats and hosts", """
        Prints one row per tool of the site file. free and total are the seats the
        probe reports; the probe runs on the head node with the project
        directory as {root}. hosts lists the hosts that have the tool, with
        their versions. A tool without a probe shows - for the seats. --json
        gives tool, free, total, hosts (host to version) and note.
        """, exits={Exit.HOSTS: "a probe failed, or printed no number"})
    s = command("metrics", "the metrics of chosen runs, as a table or a pivot", """
        Prints every metric of the chosen runs with its label, source, stage,
        step, task, name, value and unit, the file the value came from, and
        the snapshots that hold the run. Choose the runs with --source, --run,
        --label or --task; one of them is required, and they combine.
        --source is the source tag exactly as edr checkout printed it, with
        its -n... and -dirty-... parts, and may be given more than once; --run
        takes one run instead. --label takes the runs whose label or config
        has that name, and the runs that edr continue made from a run of that
        label, whose label is <label>.<stage>. --task keeps the rows of one
        task. A snapshot is the directory that an edr export event names, when
        the manifest there lists the run. --csv writes the columns of
        metrics.csv (docs/guides/results.md) to stdout. A value that breaks the pass
        rule of its metric shows FAIL next to it, and --json gives each row
        a verdict (pass, FAIL or null) and its snapshots. A row whose file
        did not parse shows failed: and the error in place of the value.

        --pivot prints one metric as a table: a row per label and source, in
        natural label order, so l4 comes before l16, and a column per task, or
        per stage and step for a metric without tasks. A row holds the run
        that label@source names. A cell without a value says why: failed when
        the task failed or its file did not parse, not in job when the run's
        spec has no such task, the run's phase when the run did not end done,
        and no value otherwise. --csv writes the pivot as CSV.

        --run with --over steps prints the metrics along the steps of that run:
        one row per step with its name, one column per metric, and a verdict
        column when a metric has a pass rule: FAIL when a value of that step
        breaks its rule. With --metric, it prints that one metric, its change
        from the step before and its source file.

        --instance or --depth prints the instance rows of an area_hier or
        table metric instead: label, source, stage, step, task, metric, part,
        instance, depth, the value with the children, the local value without
        them, the cell count when the report has one, and the unit. --instance
        is a glob over the path, in which * also matches /.

        --unit kGE or MGE prints an area in um2 in gate equivalents, by the
        ge_um2 key of edr.toml.
        """, exits={Exit.NOTHING: "no metric row"})
    s.add_argument("--source", action="append", metavar="SOURCE",
                   help="the exact source tag of the runs, as in the run id; repeatable")
    s.add_argument("--run", metavar="HANDLE", help="one run: " + HANDLE)
    s.add_argument("--label", metavar="NAME", help="the runs of this label or config, and the runs that continue them")
    s.add_argument("--task", metavar="TASK", help="the rows of one task")
    s.add_argument("--metric", metavar="NAME", help="one metric, by name or canonical name")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--over", choices=["steps"], help="with --run: the metrics along the steps")
    g.add_argument("--pivot", action="store_true", help="one metric as a table: labels and sources by task or step")
    s.add_argument("--stage", metavar="S", help="the metrics of one stage")
    s.add_argument("--step", type=int, metavar="N", help="the metrics of one step number")
    s.add_argument("--csv", action="store_true", help="CSV on stdout")
    s.add_argument("--instance", metavar="GLOB", help="the instance rows whose path matches the glob")
    s.add_argument("--depth", type=int, metavar="N", help="the instance rows at this depth; the top is 0")
    s.add_argument("--unit", choices=list(analysis.GE), help="an area in um2 in kGE or MGE, by ge_um2")
    s = command("extract", "extract the metrics of runs again from their collected files", """
        Extracts every metric in edr.toml again from the files collected for
        each run under data/results, and makes the rows of the run match the
        definitions. It uses the same function as the watcher, so it reads
        the tasks that ended done, the stages that exited 0, and in any other
        stage the steps that the run has passed: step_runs holds the step and
        a later one. A run without a heartbeat, such as an imported one, is
        read for every stage and for every task of its spec.

        New rows are added. A row is replaced when its value, canonical name,
        unit or source file has changed, or when its instance rows differ. A row
        that the extraction no longer gives is removed with its instance rows when
        it is a failed row, when no metric defines it at its stage and step,
        or when the extraction read its stage and task. A value whose file is gone is
        kept and counted. A value of a stage or task that the extraction did
        not read, such as a stage outside the run's spec or a task that
        tasks.toml no longer resolves, stays as it is. A file that does not
        parse gives a failed row: an empty value, and the error in place of
        the source file.

        It then reads the [parameters.<name>] tables from the files of the
        stages of the run's spec, replaces the run's parameters of origin
        extract with them, and runs the checks, which rewrite the flags of the
        run and the same_parameters flags of every run at its source.

        Pass exactly one of a handle, --batch or --source. For each run,
        extract prints how many rows are new, changed, unchanged, failed and
        removed, and how many it kept without a file, then a line per failing
        metric with its count and its first error, and a line per flag. The
        count line also names how many parameters the files gave and how
        many flags the run has. It writes the rows, parameters and flags of a
        run in one transaction, with an extract event of the same text.
        --dry-run prints the counts of the metric rows and writes nothing.
        With --json, data holds for each run run_id, the counts, failures:
        the count and the first error of each failing metric, and flags.
        A task id that ran with other fields in another run gets a warning:
        line with the fields that differ and up to five runs of each set.
        --json lists these under clashes of each run, with every run.
        """, write=True, exits={Exit.NOTHING: "no run matches"})
    s.add_argument("handle", nargs="?", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="every run of the batch")
    s.add_argument("--source", action="append", metavar="SOURCE", help="every run of the exact source tag; repeatable")
    s = command("compare", "two or more runs side by side", """
        Puts two or more runs side by side. Without --instances, it prints one row
        per task and metric: the value of each run with its stage and step,
        and the delta and the percent of each run to the first. A metric
        with `record` shows each run at its step of record, the deepest step
        of the record stage at or after `from`. Any other metric shows each
        run at the deepest step that every run has. With --step, every run
        is at that step. A run that lacks its step of record or the --step is
        named missing, and the command exits 2. A value that breaks the pass
        rule of its metric shows FAIL next to it. --metric (repeatable),
        --stage and --task narrow the rows; --json keeps the source file and
        the verdict of every value and lists the missing runs.

        --ref H or --base H prints the runs as rows instead, which needs one
        value per metric for each run, as one task gives. For each metric a
        row shows the value with its stage and step, the change from the row
        before, the percent against the run of --ref (default: the first
        handle) and against the run of --base, and the rank, 1 for the lowest
        value. A run of --ref or --base that is not a handle gives its
        percent and gets no row. With two or more metrics, the last column
        says differ when the metrics rank a run differently. --json adds
        prev, pct_ref, pct_base and rank to each row, and ref, base and
        ranks_differ.

        --instances puts the instances of one area_hier or table metric side
        by side: one row per instance at --depth (default 1; the top is 0),
        one column per run with its stage and step in the header, and the
        delta and the percent of each run to the first. --instance keeps the
        instances whose path matches a glob, in which * also matches /. An
        instance that a run lacks counts as 0 there, and its percent reads
        new or gone. Below the instances come <sum>, the sum of the rows
        shown, <other>, the top less that sum, and <top>; at depth 0 only
        <top>. Each run is at the step the rules above give for the metric.
        --task names the task of a task metric, and --part the part, such as
        a phase or a trace slice; without --part, the part that the metric's
        top names. When the runs have several instance metrics, --metric names
        one. A --depth deeper than the rows in the database reads the report or
        table that the metric row of each run cites. --csv writes the
        instance and the value of each run, the three rows at the end included.
        The source file of each run is printed under the table.

        --unit kGE or MGE prints an area in um2 in gate equivalents, by the
        ge_um2 key of edr.toml.

        A column is named by the label, or by label@source when the runs come
        from more than one source; then a line above the table names the
        sources, and --json sets mixed_sources. Two runs with the same name
        get a prefix of their run ids after it.

        Above the table, one line per parameter whose value differs between
        the runs gives the value of each run: the config, the build tag, an
        override, vars.<name> or nested.<name>, as the launch or the import
        recorded it. --json lists them under parameters.

        --html FILE also writes compare.html to FILE, the page the watcher
        writes to data/board/, opened on the runs of the handles with the
        first as the base of every percent. It loads Plotly from
        plotly.min.js next to FILE when that file exists, else from the CDN.
        """, exits={Exit.NOTHING: "no row to compare, or a run lacks its step of record or the --step"})
    s.add_argument("handles", nargs="+", metavar="HANDLE", help=HANDLE)
    s.add_argument("--instances", action="store_true", help="the instances of one metric side by side")
    s.add_argument("--metric", action="append", metavar="NAME", help="this metric, by name or canonical name; repeatable")
    s.add_argument("--task", metavar="TASK", help="the rows of one task; with --instances, the task of a task metric")
    s.add_argument("--part", metavar="PART", help="with --instances: the part, such as a phase; default: the part of top")
    s.add_argument("--depth", type=int, metavar="N", help="with --instances: the instance depth; default 1")
    s.add_argument("--instance", metavar="GLOB", help="with --instances: the instances whose path matches the glob")
    s.add_argument("--csv", action="store_true", help="with --instances: CSV on stdout")
    s.add_argument("--unit", choices=list(analysis.GE), help="an area in um2 in kGE or MGE, by ge_um2")
    s.add_argument("--stage", metavar="S", help="this stage only; another stage than the record stage takes the "
                                                  "deepest step the runs share")
    s.add_argument("--step", type=int, metavar="N", help="every run at this step number")
    s.add_argument("--ref", metavar="H", help="the runs as rows, with the percent against this run: " + HANDLE)
    s.add_argument("--base", metavar="H", help="the runs as rows, with the percent against this run: " + HANDLE)
    s.add_argument("--html", metavar="FILE", help="also write compare.html, opened on these runs, to FILE")
    s = command("runtime", "stage, step and task times", """
        With one handle, runtime prints the times of one run: a row per stage
        attempt from the stage_runs table, a row per step under it, and one
        row per task group with the task count, the summed task time and the
        longest task. A step starts at the time the stage's step_log finds in
        a collected file, else when the driver first saw its number. It ends
        when the next step of its stage starts, else when the stage ends, else
        at the mtime of its own log file. A stage without an end counts up to
        now while the run lives, else up to its last heartbeat. A time that
        counts up to now, or that has no end, is open; the total then reads
        "at least" and names it. The source column names the table or the
        file and line of each time. The total sums the stage attempts, or the
        steps of a run without stage rows, such as an imported one.

        With several handles or --batch, it prints one row per run: the wall
        time of each stage, with the attempts summed, the total, and an open
        column when a run has an open time. --json gives each stage and step
        row with wall_s and open, and the run with total_s and open, the list
        of its open times.
        """, exits={Exit.NOTHING: "no stage or step time"})
    s.add_argument("handles", nargs="*", metavar="HANDLE", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="every run of the batch")
    command("init", "write edr.toml here", """
        Writes edr.toml into the current directory, the project directory
        where you run edr. --site names the site directory or the site file
        itself; init does not write that file. It refuses when edr.toml
        already exists. edr serve watches the project once it is registered.
        """, write=True).add_argument("--site", required=True, metavar="DIR", help="the site directory, or a site.toml path")
    command("check", "load everything, probe the hosts, check the hooks", """
        Loads the project, the site, user.toml and every batch under jobs/,
        imports every hook, checks the driver file, probes every host once, names every tool
        the head node lacks, and plans every batch with those probes. It prints
        one problem: line per fault, or an ok: line with the counts. A
        warning: line names each task id of the database that ran with more
        than one set of fields, with up to five runs of each set.
        """, exits={Exit.REFUSED: "a problem was found"})
    command("register", "link the project into the registry", """
        Writes the link ~/.edr/projects/<project> to the project directory,
        so the commands that span projects find it. launch, continue, track,
        import and watch write it too. A name that belongs to another
        directory is refused; a link to a directory that no longer holds that
        project is replaced.
        """, write=True, exits={Exit.NOTHING: "the link exists"})
    command("unregister", "remove the link of the project from the registry", """
        Removes the link ~/.edr/projects/<project> and nothing else: the
        state, the database and the run trees stay. A link that names another
        project directory is refused.
        """, write=True, exits={Exit.NOTHING: "the project is not registered"})
    s = command("checkout", "check out a ref as a clone, or a dirty tree as a snapshot", """
        Fetches the repository, then makes a detached local clone of ref (default source.ref) at
        <worktrees>/<tag>, and clones each source.nested repository into it at
        the HEAD the repository copy has. The tag is the short hash of the
        commit, then -n<short hash> for each source.nested repository in the
        order of that list. A tag as ref checks out the commits that it names.
        A local clone shares the git objects of the repository by hard links.
        It prints <source> <path>.

        --dirty DIR clones the commits of a working tree and copies over the
        clone the files that its tag covers: the tracked files and the
        untracked files that git does not ignore, of the tree and of each
        source.nested repository. The diff of these files against the commits
        gives the tag <tag>-dirty-<8 hex> from its sha256, printed with
        (dirty). A git repository in the tree that source.nested does not name
        and git does not ignore is refused. source.diff and source.json go
        into the clone and into data/sources/<tag>/, which retire keeps. A
        clean tree under --dirty is checked out as a clone.
        """, write=True)
    s.add_argument("ref", nargs="?", help="the git ref to check out; default source.ref")
    s.add_argument("--dirty", metavar="DIR", help="snapshot this working tree instead of a ref")
    s = command("plan", "render the run specs of a batch; writes no spec", """
        Renders every job of the batch into a run spec and prints
        <run id>: <host or queued> <root> per job, with problem: lines under a
        job that cannot run. With --json, data[].spec is the full spec of each
        job.

        If the batch's source is a clean ref that has not been checked out
        yet, plan checks it out first, the same way edr checkout does, and
        prints a checkout <source> <path> line. With --dry-run it prints that line
        and the git commands but checks nothing out. Apart from that checkout,
        plan writes nothing. A dirty source that has not been checked out is
        refused; add it with edr checkout --dirty DIR.

        --show-spec also prints the rendered spec of each run: the environment
        its commands get, the command of each stage and task, and the collect
        paths.
        """, write=True, exits={Exit.REFUSED: "a job has a problem"})
    s.add_argument("batch", nargs="?", help="the batch name; default EDR_BATCH, else the newest")
    s.add_argument("--show-spec", action="store_true", help="print the env, commands and collect paths of each run")
    s = command("launch", "start one driver per job of a batch", """
        Checks out a missing clean source the way plan does, then pins the date
        of the batch, publishes the driver into the state directory, syncs the
        checked-out tree to each host, writes one spec per run
        and starts one driver per run, stagger_s apart, with a waiting line
        before each wait. Prints <n> started, <n> queued, <n> with problems.
        --show-spec prints the rendered spec of each run as plan does. A job that no host fits is
        queued; the watcher starts it when a host frees up. A job whose spec
        exists is already launched; a batch name is used once.
        """, write=True, exits={Exit.DONE: "a run started or was queued", Exit.REFUSED: "a job has a problem",
                                Exit.NOTHING: "every job was already launched"})
    s.add_argument("batch", nargs="?", help="the batch name; default EDR_BATCH, else the newest")
    s.add_argument("--only", metavar="L", help="labels, comma separated")
    s.add_argument("--allow-dirty", action="store_true", help="launch a dirty snapshot source")
    s.add_argument("--show-spec", action="store_true", help="print the env, commands and collect paths of each run")
    s = command("continue", "more work on the tree of an existing run", """
        Runs stages on the tree of an existing run, as one new run in the batch
        of that run. Without --stage it runs the stages the tree has left. Each
        run on the tree was launched with a list of stages, and the stages left
        are those after the last one that ended with exit 0, so a run that
        ended OVER_BUDGET or STOPPED at the end of a stage goes on with the next
        one. It refuses while a run on the tree has not ended. It also refuses
        when the first stage left started but did not end with exit 0; name the
        stages with --stage then, and a checkpoint with --from when the stage
        has a resume command, because a stage that runs from its start can
        delete the checkpoints it needs.

        --stage names the stages to run, in the order given. The new run gets
        the label <label>.<stage> for one stage and <label>.<first>-<last> for
        several, with .2, .3 and so on when the batch has that label already.
        --tasks names the tasks of a task group, --parallel its width, --on the
        host (default: the tree's host). --from fills {checkpoint} in the resume
        command of the first stage, and is refused when that stage has none.

        --collect NAME instead copies the collect_on_request list NAME of every
        stage from the tree into data/results/<run id>/.
        """, write=True, exits={Exit.REFUSED: "the plan has a problem, --from names a stage without resume, or "
                                              "without --stage the tree cannot go on by itself",
                                Exit.NOTHING: "without --stage, no stage is left on the tree",
                                Exit.HOSTS: "with --collect, a copy failed"})
    s.add_argument("handle", help=HANDLE)
    s.add_argument("--stage", nargs="+", metavar="S", help="the stages to run on the tree, in this order; default the "
                                                           "stages the tree has left")
    s.add_argument("--tasks", nargs="+", metavar="ID", help="the tasks of a task group; default the job's")
    s.add_argument("--from", dest="from_", metavar="CHECKPOINT", help="resume the first stage from this checkpoint")
    s.add_argument("--on", metavar="HOST", help="the host; default auto")
    s.add_argument("--parallel", type=int, metavar="N", help="tasks at once; default the stage's parallel")
    s.add_argument("--collect", metavar="NAME", help="fetch a collect_on_request list instead")
    s = command("track", "run a command under the driver here, as a run of the project", """
        Runs one command in the foreground under the driver, on this machine, and
        records it as a run in the batch --batch (default track). A lab with its
        own scheduler writes edr track into its job script; the board, the alerts,
        the metrics and export then see the run.

        The run is one stage named --stage. A stage of edr.toml with that name
        gives its steps, progress, budget, retry and tools, so the gate and the
        budget work; the command replaces its cmd. The tree is --root, default
        the current directory, and the driver writes log/<stage>.log there. The
        run id follows source.run_id with --config (default the label) as
        config, --build-tag (default track) as the build tag, and --source
        (default the source tag of the tree) as {source}. A bench run tracked
        with the build tag of a backend build joins the backend runs of that
        build on the tag, as edr coverage does.

        edr track then replaces itself with the driver: the pid, the signals
        and the exit code are the driver's. With --collect, the watcher copies
        the stage's collect paths from the tree, which the head node must read
        at the same path, and extracts the metrics when the run ends. Without
        it, the watcher collects nothing. --dry-run prints the spec and runs
        nothing.

        Once the driver runs, the exit code is the driver's, as the table
        below lists; 2 and 3 then carry the driver's meaning, not the one of
        the global table. docs/guides/run.md lists the phases.
        """, write=True, exits={Exit.DONE: "the command ended done", 2: "FAILED:setup, the stage is not in the spec; "
                                "or FAILED:<stage>, a checkpoint on a stage without resume",
                                3: "FAILED:<stage>, too little disk for the stage", 4: "FAILED:<stage>, the tool gate timed out",
                                5: "FAILED:<stage>, the command failed", 8: "INCOMPLETE, a task failed or was skipped",
                                9: "OVER_BUDGET:<stage>, a budget passed", 10: "STOPPED or KILLED:<signal>"})
    s.add_argument("--label", required=True, metavar="L", help="the label of the run")
    s.add_argument("--stage", required=True, metavar="S", help="the stage name; a stage of edr.toml lends its settings")
    s.add_argument("--batch", default="track", metavar="B", help="the batch; default track")
    s.add_argument("--config", metavar="C", help="the configuration name of the run; default the label")
    s.add_argument("--build-tag", dest="build_tag", default="track", metavar="TAG",
                   help="the build tag of the run, the tag of the backend runs it belongs to; default track")
    s.add_argument("--source", metavar="SOURCE", help="the source tag; default the tag of the tree")
    s.add_argument("--root", metavar="DIR", help="the run tree; default the current directory")
    s.add_argument("--collect", action="store_true", help="the watcher collects the stage and extracts its metrics")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
    s = command("keep", "more hours for a run, and no hung kill or superseded stop for that long", """
        Writes the keep file of a live run with N hours, 12 by default. The
        driver adds them to the time budget of the running stage or task, and
        for N hours from now the watcher takes no automatic action on the run:
        it neither kills it when it is hung nor stops it when a newer batch
        supersedes it. The full-host stop does not wait for a keep, since a
        full disk blocks every other user of the host. A new keep replaces the
        one before.
        """, write=True, exits={Exit.NOTHING: "the run has ended"})
    s.add_argument("handle", help=HANDLE)
    s.add_argument("--hours", type=int, default=12, metavar="N", help="the hours; default 12")
    s = command("import", "record a run tree that edr did not make, or its collected results", """
        Records a run that edr did not start, such as one you ran by hand. With
        --host and --root, it records the tree on that host, so reuse and edr
        continue can build on it. --results DIR names a directory of collected
        files from a run whose tree is gone; it is linked as
        data/results/<run id>, and the project's metrics are extracted from
        it; --tasks names the tasks whose files it holds. The run id must
        start with YYYYMMDD_HHMM_.

        The run holds what you tell it and nothing more. --host, --started
        and --ended give its host, start and end; without them these stay
        empty. Each --param KEY=VALUE writes a parameter of origin import, next
        to config, build_tag and source. The dirty flag is set when the source
        tag holds -dirty, and a tag that edr checkout would not make, <hash>
        with -n<hash> for each source.nested repository and an optional
        -dirty-<8 hex>, gets a warning. --phase is a phase that the
        driver ends a run with: done, INCOMPLETE:<n>f<m>s where a task failed
        or was skipped, FAILED:<stage>, OVER_BUDGET:<stage>, STOPPED or
        KILLED:<signal>. A run whose driver died is FAILED:<stage>, with the
        stage it died in.

        Each task takes its fields from tasks.toml, as a launch would, and
        the task_fields table records them with the origin resolver. When a
        task ran with other fields than tasks.toml gives today, pass them with
        --task-fields FILE: a file in the form of tasks.toml, whose
        [tasks.<id>] table replaces the fields of that task and is recorded
        with the origin import.

        Import writes the spec <state_dir>/<batch>/<run id>.spec.json in the
        format of launch, with every task group and the directory and fields
        of each task, so a later edr extract reads every imported task with
        the fields it ran with. The task counts of the run give a task whose
        files gave a value and no failed row as done, and any other task as
        failed. A batch holds one source: the launch or import that makes a
        batch sets it, and import refuses a run of another source there.
        """, write=True)
    s.add_argument("--run-id", required=True, dest="run_id", help="the run id; it must start with YYYYMMDD_HHMM_")
    s.add_argument("--label", required=True, help="the label of the run")
    s.add_argument("--config", default="", help="the configuration name of the run; default empty")
    s.add_argument("--source", required=True, metavar="SOURCE", help="the source tag of the tree")
    s.add_argument("--host", help="the host of the tree, or of the run whose files --results holds")
    s.add_argument("--started", type=_unix, metavar="TIME",
                   help="the start of the run: a unix time, or an ISO time such as 2026-08-30T09:12")
    s.add_argument("--ended", type=_unix, metavar="TIME", help="the end of the run, in the same forms")
    s.add_argument("--root", metavar="PATH", help="the tree on the host")
    s.add_argument("--results", metavar="DIR", help="collected files in the run layout; linked as data/results/<run id>")
    s.add_argument("--tasks", nargs="+", metavar="ID", help="the tasks whose files the results hold")
    s.add_argument("--task-fields", dest="task_fields", metavar="FILE",
                   help="a file in the form of tasks.toml with the fields that the tasks ran with")
    s.add_argument("--batch", default="imported", help="the batch to record it in; default imported")
    s.add_argument("--phase", default="done", help="the phase the run ended with, as the driver writes it; default done")
    s.add_argument("--build-tag", dest="build_tag", metavar="TAG", help="the build tag of the run")
    s.add_argument("--param", action="append", type=_param, metavar="KEY=VALUE",
                   help="a parameter of the run, such as vars.netlist_stage=11; repeatable")
    s.add_argument("--why", default="", help="the reason; it goes into the events table")
    s = command("export", "a frozen snapshot of one or more sources", """
        Writes a snapshot of the sources to DIR: manifest.json, runs.csv,
        metrics.csv, parameters.csv, task_fields.csv, instances.csv and
        flags.csv of one run per label and source, the newest run by start
        time that ended done, else the newest run. --source matches the
        source tag exactly and may be given more than once. The manifest lists
        the exported runs whose phase is not done under incomplete, the other
        runs of each label and source under skipped, and each dirty source
        under dirty_sources, whose diff goes to sources/<tag>/source.diff.

        The collected files come only on request. --files copies the files
        that the exported metric rows cite, and --with GLOB the files of each
        run that match GLOB under data/results/<run_id>/, where * matches
        within a name and ** across directories. A file goes to
        <run_id>/<path>, and its entry in the manifest names the run, stage,
        step and task. It refuses a DIR that exists and is not empty.
        docs/guides/results.md explains the layout.

        --mlflow DIR writes the project database into a local MLflow tracking store
        in DIR instead (mlflow.db and artifacts/), for mlflow ui: one MLflow run
        per run, of every source or of each --source, with the parameters, the
        metrics at their step, the stage and step times, and the collected
        files up to 1 MiB. A run already in the store is skipped. It needs the
        mlflow extra: pip install 'edarunner[mlflow]'.
        """, write=True)
    s.add_argument("--source", action="append", metavar="SOURCE",
                   help="the exact source tag of the runs, as in the run id; repeatable")
    s.add_argument("--out", metavar="DIR", help="the directory to write; it must be absent or empty")
    s.add_argument("--mlflow", metavar="DIR", help="write an MLflow tracking store in DIR instead")
    s.add_argument("--labels", metavar="a,b", help="these labels only, comma separated")
    s.add_argument("--files", action="store_true", help="also copy the collected files that the exported metric rows cite")
    s.add_argument("--with", dest="globs", action="append", metavar="GLOB",
                   help="also copy the collected files of each run that match GLOB, such as run.json or '**/*.rpt'; repeatable")
    command("coverage", "whether a run holds each test of a demand list", """
        Reads DEMAND.csv, the list of tests an analysis needs, and says for
        each row whether a run holds it. The header names the columns label or
        build_tag, stage and task, and optionally source; other columns are
        ignored. A row matches the runs with its label, its build tag, or both.
        A label matches as edr metrics --label does: the runs whose label or
        config has that name, and the runs that edr continue made from a run
        of that label. An empty task means the numbers of the stage itself,
        and an empty source means any source.

        For each label and source, coverage takes one run, the one that
        label@source names: the newest run by start time that ended done, else
        the newest run. A row is held when that run at the row's source has a
        value at the row's stage and task. Otherwise the status is the first of
        these that fits: running when that run has not ended, failed when it
        ended in a phase other than done, elsewhere when a run at another source
        holds the row, and else missing. The runs column names the runs behind
        the status as label@source, with the phase when it is not done.

        A build tag matches every run of one build: the backend runs, the runs
        that continue them, and a bench suite imported with the same build tag.
        --json gives each row with its status and runs.
        """, exits={Exit.REFUSED: "a row is not held, or DEMAND.csv is unreadable or incomplete"}).add_argument(
        "demand", metavar="DEMAND.csv", help="the demand list: label or build_tag, stage, task, and an optional source")
    s = command("stop", "stop one run", """
        Stops one run through the pids the driver recorded, never through a
        session name or a process pattern. Without a flag, it sends SIGTERM to
        the driver and every process group of the run and waits up to 60 s.
        --after-task writes the stop file: a task group finishes its running
        tasks and claims no more, and a one-command stage runs to its end. --now
        sends SIGTERM, then SIGKILL after 30 s.

        A queued run is marked stopped and never starts. A run whose heartbeat
        has no driver pid is refused.
        """, write=True, why=True, exits={Exit.NOTHING: "the run has ended",
                                          Exit.HOSTS: "the driver is still alive after the wait; use --now"})
    s.add_argument("handle", help=HANDLE)
    s.add_argument("--after-task", action="store_true", help="write the stop file; the running task ends first")
    s.add_argument("--now", action="store_true", help="SIGKILL after 30 s")
    s = command("retire", "remove the run tree, or its prune targets", """
        Removes the run tree on the host, or with --prune T the paths that
        prune.T names in the stages, after the guard on every target; T may
        name several sets, comma separated. --batch retires every run of the
        batch and marks it RETIRED, so the watcher skips it. A live run gets the
        phase ABANDONED:<why>. --host H with --prune prunes every finished run
        of the project that has a tree on H, to free the scratch of a full host;
        the Free space button of a host_full alert runs it with every set the
        project declares. It skips a run whose tree has stages left, the ones
        edr continue would run, since those stages may need the files, and
        prints a line for each run it skips. A run that you name is pruned all
        the same, and retire prints the stages its tree has left above its rm
        lines.

        The logs and results survive a retire. The watcher has already copied
        log/ and the collect paths of every finished stage to
        data/results/<run id>/ on the head node, and retire refuses a tree
        without that copy unless you pass --uncollected. edr watch --once makes the copy
        now.

        --collect NAME,... first copies the named collect_on_request lists of
        every run into data/results/<run id>/, and removes nothing when one copy
        failed.

        retire refuses a run whose driver is alive, a live run that has no
        heartbeat yet and started less than dead_s ago, a tree that another
        live run uses, a tree shared with a run whose results are not collected
        (retire them together with --batch), and a tree whose own results are
        not collected unless you pass --uncollected.
        """, write=True, why=True, exits={Exit.NOTHING: "the batch has no run, or no finished run without stages left "
                                                        "has a tree on the host",
                                          Exit.HOSTS: "an rm failed"})
    s.add_argument("handle", nargs="?", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="every run of the batch, then mark it RETIRED")
    s.add_argument("--collect", metavar="NAMES", help="copy these collect_on_request lists, comma separated, to the head node first")
    s.add_argument("--prune", metavar="T", help="remove the prune targets named T instead of the tree; comma separated")
    s.add_argument("--host", metavar="H", help="with --prune: every finished run of the project with a tree on H and "
                                               "no stage left")
    s.add_argument("--uncollected", action="store_true", help="remove a tree whose results were never collected")
    s = command("notify", "send one message, the board or the digest through every notifier", """
        Sends one message through every notifier that user.toml configures. The
        message starts with a header line with the project name, like every
        message of the bot, and TEXT follows as plain text. --board sends the
        board of edr status and --digest the digest of every registered project
        instead, so a cron line can mail either.
        """, write=True, exits={Exit.REFUSED: "no notifier is configured, a send failed, or not exactly one "
                                              "of TEXT, --board and --digest"})
    s.add_argument("text", nargs="?", help="the message, as plain text")
    s.add_argument("--board", action="store_true", help="send the board of edr status")
    s.add_argument("--digest", action="store_true", help="send the digest of every project now; the daily one still comes")
    s.add_argument("--silent", action="store_true", help="send without a sound on the phone")
    s = command("watch", "the watcher", """
        Runs the watcher loop: one cycle every heartbeat_s seconds. --once runs one cycle.
        --check reads the watcher's own heartbeat; a cron line runs it. --dry-run
        reads and classifies every run, prints the states and writes nothing.
        docs/how-it-works.md explains the cycle.

        A watcher holds <state_dir>/watch.lock while it runs, so a second
        watcher of the project, the loop or --once, exits 2 and names the pid
        of the first. When edr.toml stops loading, the watcher sends one alert
        per error text and goes on with the last config that loaded: it reads
        the heartbeats, alerts and collects, but resumes and launches nothing
        until the file loads again.

        Every watcher sends its alerts. When no supervisor runs, the first
        watcher that takes ~/.edr/serve.lock also does the work of the user
        and runs the Telegram bot for the commands and button presses of every
        project.
        """, write=True, exits={Exit.REFUSED: "with --once, the cycle failed or the config did not load; "
                                              "with --check, watch.json is older than three cycles",
                                Exit.NOTHING: "another process watches the project"})
    s.add_argument("--once", action="store_true", help="one cycle; exit 1 when it failed")
    s.add_argument("--check", action="store_true", help="exit 1 when watch.json is older than three cycles")
    s.add_argument("--served", action="store_true", help="started by edr serve: no census, no pinned board, no bot")
    s = command("serve", "the supervisor: one watcher per registered project", """
        Runs the supervisor of the user: a cycle every minute that keeps one
        edr watch --served per registered project in the project directory,
        does the work that belongs to the user once for every project, edits
        one pinned global board, and runs the Telegram bot for the commands and
        button presses of every project. A watcher that exits starts again after
        1, 2, 4, 8, 16 and at most 30 minutes, with one alert. A watcher whose
        watch.json stood still for three heartbeats and at least 15 minutes
        while its config loads is killed and started again, with an alert. A
        project whose files do not load has no watcher until they load, and
        gets one alert per error text. The supervisor holds
        ~/.edr/serve.lock, so a second one exits 2, and it writes
        ~/.edr/serve.json at the end of every cycle. Under systemd it sends
        READY=1 and, every cycle, WATCHDOG=1.

        --once runs one cycle: one edr watch --once --served per project, then
        the work of the user, and exits. --dry-run lists the registered
        projects and what the supervisor would do for each, and writes
        nothing. --check reads serve.json and nothing of any project; when
        the file is missing or older than three cycles, it alerts and exits
        1, so a cron line can run it.

        --unit prints the systemd user unit. When this edr runs from a
        checkout, an editable install, it first installs a copy of the
        checkout's HEAD commit with uv tool install, and the unit runs that
        copy, so a later edit or git pull in the checkout never changes the
        running supervisor. docs/guides/run.md shows the install.
        """, write=True, exits={Exit.REFUSED: "with --check, serve.json is older than three cycles; with --unit, "
                                              "the install failed",
                                Exit.NOTHING: "another supervisor runs; with --dry-run, no project is registered"})
    s.add_argument("--once", action="store_true", help="one cycle, then exit")
    s.add_argument("--check", action="store_true", help="exit 1 with an alert when serve.json is older than three cycles")
    s.add_argument("--unit", action="store_true", help="install a pinned copy when edr runs from a checkout, and print "
                                                         "the systemd user unit that runs it")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else Exit.REFUSED
    c, buf = Ctx(a), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf) if a.json else contextlib.nullcontext():
            if a.command in _REGISTERS and not a.dry_run:
                home.register(c.project.project, c.project.root)
            code = a.fn(c, a)
    except (Refuse, ConfigError, checkout.CheckoutError, runid.GitError) as e:
        print(f"edr: {e}", file=sys.stderr)
        code = Exit.REFUSED
    except HostError as e:
        print(f"edr: {e}", file=sys.stderr)
        code = Exit.HOSTS
    except KeyboardInterrupt:
        code = Exit.INTERRUPTED
    except Exception as e:  # the exit code and the --json envelope must survive any fault
        logging.getLogger("edr").exception("unhandled")
        print(f"edr: {e}", file=sys.stderr)
        code = Exit.REFUSED
    finally:
        c.close()
    if a.json:
        print(json.dumps({"code": code, "data": c.data, "output": buf.getvalue()}, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())

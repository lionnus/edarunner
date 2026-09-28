"""The `edr` command line.

Every command wires the modules; nothing here knows a file format. Exit
codes: 0 done, 1 refused or bad input, 2 nothing to do, 3 some hosts
failed. `--json` prints {"code", "data", "output"} with the captured text.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import logging
import os
import posixpath
import shlex
import shutil
import socket
import sys
import textwrap
import time
from dataclasses import asdict, replace
from enum import IntEnum
from pathlib import Path
from string import Template
from typing import Any

from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

from . import __version__, analysis, board, brief, checkout, collect, config, export, launch, metrics, runid, sync, watch
from .backend import Backend, Handle, Live, gone, make_backend, run_handle
from .config import ConfigError
from .db import Database, network_fs
from .guards import Refuse, assert_run_id, assert_safe_target
from .hosts import HostError, HostProbe, Ssh
from .model import SCHEDULERS, Batch, Job, Project, Stage
from .notify import make_notifiers, untag
from .notify.digest import Digest
from .notify.telegram import format as tgfmt

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


# These commands never create data/edr.db; `notify` reads the bot's message ids only.
_READ_COMMANDS = frozenset({"brief", "status", "events", "hosts", "tools", "metrics", "compare", "runtime", "check", "notify", "plan"})


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
            cwd = Path(os.getcwd())
            root = next((p for p in (cwd, *cwd.parents) if (p / "edr.toml").is_file()), None)
            if root is None:
                raise Refuse(f"no edr.toml in {cwd} or above; run edr init")
            self._project = config.load_project(root)
        return self._project

    @property
    def db(self) -> Database:
        if self._db is None:
            path = self.project.data / "edr.db"
            # A read command or a dry run creates nothing, not even an empty database.
            memory = not path.exists() and (self.a.dry_run or self.a.command in _READ_COMMANDS)
            self._db = Database(":memory:") if memory else Database(path)
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
        dirs = [p for p in self.project.state_dir.glob("*") if p.is_dir() and p.name not in ("bin", "leases")]
        if not dirs:
            raise Refuse(f"no batch given and no batch directory in {self.project.state_dir}")
        return max(dirs, key=lambda p: p.stat().st_mtime).name

    def batch(self, name: str | None, dry_run: bool = False) -> Batch:
        """Load a batch, check its source out when it is missing, and resolve the source to its tag."""
        b = config.load_batch(self.project, self.batch_name(name))
        got = checkout.ensure(self.project, b.source, dry_run)
        if got:
            print(f"checkout {got.source} {got.path}" + (" (dry)" if dry_run else ""))
            if not checkout.SOURCE_RE.match(b.source):
                b.source = got.source
        return self.resolve_source(b)

    def resolve_source(self, b: Batch) -> Batch:
        """A ref as source becomes the source tag of its checked-out tree; CheckoutError when it is not checked out."""
        if not checkout.SOURCE_RE.match(b.source):
            b.source = runid.source_tag(checkout.find(self.project, b.source))
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
        """The database row of label@batch, a run id prefix, or #n from the last board."""
        try:
            run_id = self.db.resolve(handle, self.db.get_store("last_board"))
        except KeyError as e:
            raise Refuse(str(e)) from None
        row = self.db.run(run_id)
        if row is None:
            raise Refuse(f"{handle}: no run {run_id}")
        return row

    def heartbeat(self, row: Row) -> dict:
        """The heartbeat of a run, or {} when the driver wrote none."""
        return config.load_json(self.project.state_dir / str(row["batch"]) / f"{row['run_id']}.json")

    def save_board(self, rows: list[Row]) -> None:
        """Keep the board order in the database, so #n resolves next time."""
        if rows:
            self.db.set_store("last_board", [r["run_id"] for r in board.order(rows)])


# --- texts shared by the commands and the bot

def _events_table(c: Ctx, events: list[Row]) -> Table | str:
    names = {r["run_id"]: f"{r['label']}@{r['batch']}" for r in c.db.runs()}
    body = [[time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), e["actor"], names.get(e["run_id"], e["run_id"] or "-"),
             board.state_text(e["kind"]), e["text"]] for e in events]
    return board.table(["time", "actor", "run", "kind", "text"], body, styles={"time": "dim", "run": "bold"}) if body else "no events"


def _probe_rows(c: Ctx) -> list[Row]:
    out: list[Row] = []
    for host in c.project.site.hosts:
        try:
            out.append(asdict(c.ssh.probe(host)))
        except HostError as e:
            out.append({"host": host, "error": str(e)})
    return out


def _mark_hosts(c: Ctx, rows: list[Row]) -> list[Row]:
    """Give each probe row its `marks`; sort the rows by the worst mark, black and red first, then by host."""
    for r in rows:
        r["marks"] = board.host_marks(None if "error" in r else HostProbe(**r), c.project.site.marks)
    return sorted(rows, key=lambda r: (board.SEVERITY.index(board.worst_mark(r["marks"].values())), r["host"]))


def _hosts_table(rows: list[Row], narrow: bool) -> Table:
    """One row per host: the worst mark, then used or free of total with a mark, a bar for the cores and the scratch."""
    head = ["ok", "host", "cores", "ram GB", "scratch GB", "gpu"] if narrow else [
        "ok", "host", "cores", "", "load", "ram GB", "mount", "scratch GB", "", "gpu", "gpu GB", "tools", "runs"]
    body = []
    for r in rows:
        m = r["marks"]
        ok = board.worst_mark(m.values())
        if "error" in r:
            body.append([ok, r["host"], Text("error: " + r["error"], style="red", justify="left")])
            continue
        cores, used = r["cores"], max(0, min(r["cores"], round(r["load"])))
        sep = "" if narrow else " "  # 48 columns leave no room for the space
        cpu, ram = f"{m['cores']}{sep}{used}/{cores}", f"{m['ram']}{sep}{r['free_ram_gb']:g}/{r['total_ram_gb']:g}"
        disk = f"{m['scratch']}{sep}{r['free_gb']:g}/{r['total_gb']:g}"
        gpu = f"{m['gpu']}{sep}{r['gpus_idle']}/{r['gpus']}" if r["gpus"] else "-"
        if narrow:
            body.append([ok, r["host"], cpu, ram, disk, gpu])
            continue
        body.append([ok, r["host"], cpu, board.bar(used, cores), f"{r['load']:g}", ram, r["mount"], disk,
                     board.bar(r["total_gb"] - r["free_gb"], r["total_gb"]), gpu,
                     f"{r['gpu_total_gb'] - r['gpu_used_gb']:g}/{r['gpu_total_gb']:g}" if r["gpus"] else "-",
                     f"{r['our_tool_procs']}/{r['other_tool_procs']}", r["our_runs"]])
    t = board.table(head, body, styles={"host": "bold", "mount": "dim"},
                    right=("cores", "load", "ram GB", "scratch GB", "gpu", "gpu GB", "tools", "runs"))
    if narrow:
        # One space between columns, and only the cores column, which holds an error, folds.
        t.padding = (0, 0)
        for col in t.columns:
            col.no_wrap = col.header != "cores"
    return t


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


def _metric_key(m: Row) -> str:
    return (m.get("canonical") or m["name"]) + (f"[{m['task']}]" if m.get("task") else "")


def _final_metrics(c: Ctx, rows: list[Row]) -> dict[str, dict[str, tuple[int, Any]]]:
    """{metric key: {run id: (step, value)}} with the last step of each run."""
    final: dict[str, dict[str, tuple[int, Any]]] = {}
    for m in c.db.metrics(run_ids=[r["run_id"] for r in rows]):
        cur, step = final.setdefault(_metric_key(m), {}), -1 if m.get("step") is None else int(m["step"])
        if step >= cur.get(m["run_id"], (-2, None))[0]:
            cur[m["run_id"]] = (step, m["value"])
    return final


def _metrics_table(rows: list[Row]) -> Table | str:
    body = [[m.get("label"), m.get("source"), m["stage"], m.get("step"), m.get("task") or "", m["name"], m["value"],
             m.get("unit")] for m in rows]
    return board.table(["label", "source", "stage", "step", "task", "metric", "value", "unit"], body,
                       styles={"label": "bold", "source": "dim"}, right=("step", "value")) if rows else "no metrics"


def _keep(c: Ctx, row: Row, hours: int | None, ack: bool | None, actor: str) -> str:
    """Write the keep file next to the spec; a field not given keeps its current value."""
    run_id = row["run_id"]
    path = c.project.state_dir / str(row["batch"]) / f"{run_id}.keep.json"
    cur = config.load_json(path)
    data = {"hours": cur.get("hours", 0) if hours is None else hours,
            "ack": bool(cur.get("ack")) if ack is None else ack}
    note = f"keep {data['hours']} h" + (", ack" if data["ack"] else "")
    if not c.a.dry_run:
        config.save_json(path, data)
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
        return {"handle": board.handle(row), "run_id": row["run_id"], "run_root": str(row.get("root") or ""),
                "host": str(row.get("host") or "")}

    def keep(self, handle: str, hours: int, actor: str) -> str:
        row = self.c.resolve(handle)
        return f"{board.handle(row)}: " + _keep(self.c, row, hours, None, actor)

    def ack(self, handle: str, actor: str) -> str:
        row = self.c.resolve(handle)
        return f"{board.handle(row)}: " + _keep(self.c, row, None, True, actor)

    def stop_after_task(self, handle: str, actor: str, why: str) -> str:
        c, row = self.c, self.c.resolve(handle)
        if not board.is_live(row):
            return f"{board.handle(row)} already {row['phase']}"
        launch.stop(c.ssh, c.db, row, {}, after_task=True, why=why, state=c.project.state_dir, actor=actor)
        return f"{board.handle(row)} stops after its task"

    def status_text(self, handle: str | None = None) -> str:
        """The board, or the state, stage, step, host, age, next command and last log line of one run."""
        if handle is None:
            rows = self.c.rows()
            self.c.save_board(rows)
            return tgfmt.board(rows, totals=metrics.step_totals(self.c.project))
        row = self.c.resolve(handle)
        self.c.refresh(str(row["batch"]))
        row = self.c.db.run(row["run_id"]) or row
        return tgfmt.run_detail(row, self.c.heartbeat(row), time.time())

    def events_text(self, n: int) -> str:
        """The last `n` events, newest first."""
        return tgfmt.events(self.c.db.events(n=n), {r["run_id"]: board.handle(r) for r in self.c.db.runs()})

    def hosts_text(self) -> str:
        """One line per host, the worst mark first."""
        return tgfmt.hosts(_mark_hosts(self.c, _probe_rows(self.c)))

    def tools_text(self) -> str:
        """One line per tool."""
        return tgfmt.tools(_tool_rows(self.c))

    def digest_text(self) -> str:
        """The daily digest now, as Telegram HTML."""
        self.c.refresh()
        return Digest(self.c.project, self.c.db).text(time.time())

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
        return _metrics_csv(self.c.db.metrics(source=source)).encode()

    def compare_text(self, handles: list[str]) -> str:
        """One block per metric: its name, then one `label value` line per run."""
        rows = [self.c.resolve(h) for h in handles]
        final = _final_metrics(self.c, rows)
        if not final:
            return "no metrics"
        out = []
        for key, cur in sorted(final.items()):
            out.append(key[:40])
            out += board.cols(["", ""], [["  " + str(r["label"]), cur.get(r["run_id"], (0, None))[1]] for r in rows]
                              ).splitlines()[1:]
        return "\n".join(out)

    def metric_text(self, name: str, source: str | None) -> str:
        """`label source step value` per metric row."""
        rows = self.c.db.metrics(source=source, name=name)
        body = [[str(m.get("label")) + (f"[{m['task']}]" if m.get("task") else ""), m.get("source"), m.get("step"),
                 f"{m['value']}{' ' + m['unit'] if m.get('unit') else ''}"] for m in rows]
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
        text = Actions(c).digest_text()
        c.emit(untag(text), {"digest": text})
        return Exit.DONE
    if a.handle:
        row = c.resolve(a.handle)
        c.refresh(str(row["batch"]))
        row = c.db.run(row["run_id"]) or row
        hb, run_id = c.heartbeat(row), row["run_id"]
        stages = sorted(c.db.stage_runs(run_id), key=lambda r: (r["stage"] != "setup", r["stage"], r["task"], r["attempt"]))
        mets = c.db.metrics(run_ids=[run_id])
        samples = c.db.run_samples(run_id)
        c.emit(board.run_detail(row, stages, mets, str(hb.get("last_log") or ""), gate=hb.get("gate"), samples=samples),
               {"run": row, "heartbeat": hb, "stages": stages, "metrics": mets, "samples": samples})
        return Exit.DONE
    code = Exit.DONE
    while True:
        rows = c.rows(a.batch or os.environ.get("EDR_BATCH"))
        if a.live:
            code = max(code, _mark_live(c, rows))
        c.save_board(rows)
        text = _triage(c, rows) if a.triage else board.narrow_text(rows) if a.narrow else board.wide(rows)
        if a.watch and not a.json:
            c.console.clear()
        c.emit(text, {"runs": rows})
        if not a.watch or a.json:
            return code
        time.sleep(c.project.limits.heartbeat_s)


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
    lines = []
    for r in board.order(rows):
        state = board.state_of(r)
        cmd = board.triage_cmd(r, state, c.heartbeat(r) if state == "dead" else {})
        if cmd is None:
            continue
        lines.append(Text.assemble((f"{state:<11}", board.STYLE.get(state, "")), " ", (f"{board.handle(r):<28}", "bold"),
                                   f" {r.get('phase') or '-'}\n    {cmd}"))
    return Text("\n").join(lines) if lines else "nothing to triage"


def cmd_events(c: Ctx, a: argparse.Namespace) -> int:
    """The last events, filtered by time and run."""
    since = _since(a.since) if a.since else None
    run_id = c.resolve(a.run)["run_id"] if a.run else None
    events = c.db.events(since_s=since, run_id=run_id, n=a.n)
    c.emit(_events_table(c, events), events)
    return Exit.DONE if events else Exit.NOTHING


def cmd_hosts(c: Ctx, a: argparse.Namespace) -> int:
    """Probe every site host, or print the samples the watcher kept."""
    if a.history:
        rows = analysis.host_history(c.db.host_samples(_since(a.since)))
        c.emit(analysis.host_history_view(rows), rows)
        return Exit.DONE if rows else Exit.NOTHING
    rows = _mark_hosts(c, _probe_rows(c))
    runs = board.live_per_host(c.rows())
    for r in rows:
        r["our_runs"] = runs[r["host"]]
    if a.narrow:
        # A long cell, such as an error, folds inside its column instead of widening the table.
        c.console.width = 48
    c.emit(_hosts_table(rows, a.narrow), rows)
    return Exit.HOSTS if any("error" in r for r in rows) else Exit.DONE


def cmd_tools(c: Ctx, a: argparse.Namespace) -> int:
    """Every site tool: the seats its probe reports from the head node, and the hosts that have it."""
    rows = _tool_rows(c)
    c.emit(_tools_table(rows), rows)
    return Exit.HOSTS if any("note" in r for r in rows) else Exit.DONE


def _metrics_csv(rows: list[Row]) -> str:
    """Metric rows as CSV with the columns of an export."""
    return export.to_csv(export.METRIC_COLUMNS, [export.metric_row(m) for m in rows]).decode()


def _area_table(rows: list[Row]) -> Table | str:
    body = [[m.get("label"), m.get("source"), m["stage"], m.get("step"), m["instance"], m["depth"], m["area"],
             m.get("local_area"), m.get("cells")] for m in rows]
    return board.table(["label", "source", "stage", "step", "instance", "depth", "area", "local", "cells"], body,
                       styles={"label": "bold", "source": "dim"},
                       right=("step", "depth", "area", "local", "cells")) if rows else "no area rows"


def cmd_metrics(c: Ctx, a: argparse.Namespace) -> int:
    """The metrics of one source, as a table or CSV; with --instance or --depth, its area rows."""
    if a.instance is not None or a.depth is not None:
        run_ids = [c.resolve(a.run)["run_id"]] if a.run else None
        rows = c.db.area(run_ids=run_ids, source=a.source, stage=a.stage, step=a.step, instance=a.instance,
                         depth=a.depth)
        c.emit(_area_table(rows), rows)
        return Exit.DONE if rows else Exit.NOTHING
    if not a.source and not a.run:
        raise Refuse("metrics needs --source or --run")
    run_ids = [c.resolve(a.run)["run_id"]] if a.run else None
    if a.over:
        if not run_ids:
            raise Refuse("--over steps needs --run")
        rows = analysis.over_steps(c.project, c.db.metrics(run_ids=run_ids, stage=a.stage, name=a.metric))
        c.emit(analysis.over_steps_view(rows), rows)
        return Exit.DONE if rows else Exit.NOTHING
    rows = c.db.metrics(source=a.source, stage=a.stage, step=a.step, name=a.metric, run_ids=run_ids)
    if a.csv and not a.json:
        sys.stdout.write(_metrics_csv(rows))
        c.data = rows
    else:
        c.emit(_metrics_table(rows), rows)
    return Exit.DONE if rows else Exit.NOTHING


def cmd_extract(c: Ctx, a: argparse.Namespace) -> int:
    """Extract every configured metric again from the collected files of runs, as the watcher does."""
    if sum(map(bool, (a.handle, a.batch, a.source))) != 1:
        raise Refuse("extract needs one of a handle, --batch or --source")
    if a.handle:
        runs = [c.resolve(a.handle)]
    else:
        runs = [r for r in c.db.runs(batch=a.batch) if not a.source or r["source"] == a.source]
    out, lines = [], []
    for run in runs:
        old = {(m["stage"], m["step"], m["task"], m["name"]): m for m in c.db.metrics(run_ids=[run["run_id"]])}
        areas: dict[tuple, set] = {}
        for x in c.db.area(run_ids=[run["run_id"]]):
            areas.setdefault((x["stage"], x["step"], x["name"]), set()).add(
                (x["instance"], x["depth"], x["area"], x["local_area"], x["cells"]))
        n = {"run_id": run["run_id"], "new": 0, "changed": 0, "unchanged": 0, "failed": 0}
        rows = watch.extract_run(c.project, c.db, run, c.heartbeat(run), actor=None if a.dry_run else "user")
        for r in rows:
            m = old.get((r["stage"], r["step"], r["task"], r["name"]))
            area = {(i["instance"], i["depth"], i["area"], i["local_area"], i["cells"]) for i in r.get("instances") or []}
            same = (m is not None and (m["value"], m["canonical"], m["unit"]) == (r["value"], r["canonical"], r["unit"])
                    and area <= areas.get((r["stage"], r["step"], r["name"]), set()))
            kind = "failed" if r["value"] is None else "new" if m is None else "unchanged" if same else "changed"
            n[kind] += 1
            if not a.dry_run and kind in ("new", "changed"):
                c.db.add_metric(r, replace=True)
        text = f"{n['new']} new, {n['changed']} changed, {n['unchanged']} unchanged, {n['failed']} failed"
        if not a.dry_run:
            c.db.add_event("user", run["run_id"], "extract", text)
        out.append(n)
        lines.append(f"{run['run_id']}: {text}" + (" (dry)" if a.dry_run else ""))
    c.emit("\n".join(lines) or "no runs", out)
    return Exit.DONE if runs else Exit.NOTHING


def cmd_compare(c: Ctx, a: argparse.Namespace) -> int:
    """Two or more runs side by side."""
    runs = [c.resolve(h) for h in a.handles]
    if not a.area:
        mets = [m for m in c.db.metrics(run_ids=[r["run_id"] for r in runs], stage=a.stage, step=a.step)
                if not a.metric or m["name"] in a.metric or m.get("canonical") in a.metric]
        rows = analysis.side_by_side(c.project, runs, mets)
        c.emit(analysis.side_by_side_view(runs, rows), {"runs": runs, "rows": rows})
        return Exit.DONE if rows else Exit.NOTHING
    picked, rows = analysis.area_delta(c.db, runs, a.depth, a.instance, a.stage, a.step)
    c.emit(analysis.area_view(picked, rows, a.depth), {"runs": picked, "depth": a.depth, "rows": rows})
    return Exit.DONE if rows else Exit.NOTHING


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
    """Write edr.toml and the watch unit into the current directory."""
    cwd = Path(os.getcwd())
    if (cwd / "edr.toml").exists():
        raise Refuse(f"{cwd / 'edr.toml'} exists; edit it or remove it first")
    site = Path(a.site).expanduser()
    if site.suffix != ".toml":
        site = site / "site.toml"
    values = {"project": cwd.name, "site": str(site), "project_root": str(cwd),
              "edr": shutil.which("edr") or f"{sys.executable} -m edarunner.cli"}
    written = []
    for name in ("edr.toml", "edr-watch.service"):
        if (cwd / name).exists():
            continue
        text = Template((TEMPLATES / name).read_text()).substitute(values)
        if not a.dry_run:
            (cwd / name).write_text(text)
        written.append(str(cwd / name))
    c.emit("\n".join(f"write {w}" + (" (dry)" if a.dry_run else "") for w in written),
           {"written": written, "site": str(site)})
    return Exit.DONE


def cmd_check(c: Ctx, a: argparse.Namespace) -> int:
    """Load every file, probe the hosts, import the hooks, plan every batch."""
    problems: list[str] = []
    try:
        project = c.project
    except ConfigError as e:
        c.emit(f"problem: {e}", {"problems": [str(e)]})
        return Exit.REFUSED
    hooks = [project.source.build_tag, project.task_resolver, *(m.python for m in project.metrics.values())]
    for spec in filter(None, hooks):
        try:
            config.load_hook(project.root, spec)
        except ConfigError as e:
            problems.append(f"hook {spec}: {e}")
    if not launch.DRIVER_SRC.is_file():
        problems.append(f"driver missing: {launch.DRIVER_SRC}")
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


def cmd_continue(c: Ctx, a: argparse.Namespace) -> int:
    """Run one stage on the tree of an existing run, or fetch a collect_on_request list."""
    row = c.resolve(a.handle)
    project, run_id = c.project, row["run_id"]
    if a.collect:
        res = collect.collect_on_request(project, c.ssh, c.db, row, a.collect, a.dry_run)
        if not a.dry_run:
            c.db.add_event("user", run_id, "collect", f"{a.collect}: {res.files} files, {len(res.failures)} failed")
        c.emit("\n".join([f"{run_id}: {res.files} files" + (" (dry)" if a.dry_run else ""), *res.failures]), asdict(res))
        return Exit.HOSTS if res.failures else Exit.DONE
    if not a.stage:
        raise Refuse("continue needs --stage or --collect")
    if a.stage not in project.stages:
        raise Refuse(f"unknown stage {a.stage}")
    try:
        batch = config.load_batch(project, str(row["batch"]))
        job = next((j for j in batch.jobs if j.label == row["label"]), None)
    except (ConfigError, OSError):
        # An imported tree has no jobs file; the database row is the job.
        batch, job = None, None
    if job is None:
        job = Job(label=str(row["label"]), config=str(row.get("config") or ""))
        batch = Batch(batch=str(row["batch"]), source=str(row["source"] or ""), jobs=[job],
                      path=project.root / "jobs" / f"{row['batch']}.toml")
    job.reuse, job.stages, job.host = {"run_id": run_id}, [a.stage], a.on or "auto"
    if a.tasks:
        job.tasks = a.tasks
    if a.parallel:
        project.stages[a.stage].parallel = a.parallel
    # The run joins the batch of the tree it continues; the stage in the label and the time keep its id apart.
    job.label = f"{job.label}.{a.stage}"
    batch.batch, batch.jobs = str(row["batch"]), [job]
    state = project.state_dir
    (p,) = launch.plan(project, batch, c.ssh, c.db, date=time.strftime(launch.DATE_FMT), backend=c.backend)
    if p.problems:
        c.emit("\n".join(f"{p.run_id}: problem: {x}" for x in p.problems), {"problems": p.problems})
        return Exit.REFUSED
    if a.from_:
        if not p.spec["stages"][0].get("resume"):
            raise Refuse(f"stage {a.stage} has no resume command; --from needs one")
        p.spec["start_at"]["checkpoint"] = a.from_
    driver = sync.publish_driver(state, launch.DRIVER_SRC, a.dry_run)
    spec_path = launch.write_spec(state, batch.batch, p, driver, a.dry_run)
    c.emit(f"{p.run_id}: {a.stage} on {p.host} {p.root}" + (" (dry)" if a.dry_run else ""),
           {"run_id": p.run_id, "batch": batch.batch, "host": p.host, "root": p.root, "spec": p.spec})
    if a.dry_run:
        return Exit.DONE
    c.db.upsert_run({**launch.run_row(p, batch), "phase": "setup", "state": "running", "started": int(time.time())})
    c.db.add_event("user", p.run_id, "continue", f"{a.stage} on {run_id}" + (f" from {a.from_}" if a.from_ else ""))
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
        source = a.source or runid.source_tag(root)
    except runid.GitError:
        raise Refuse(f"{root} is not a git tree; pass --source") from None
    date, host = time.strftime(launch.DATE_FMT), socket.gethostname()
    job = Job(label=a.label, config=a.label)
    batch = Batch(batch=a.batch, source=source, jobs=[job], path=project.root / "jobs" / f"{a.batch}.toml")
    v = config.placeholders(project, date=date, batch=a.batch, label=a.label, config=a.label, build_tag="track",
                            source=source, overrides={})
    run_id = config.render(project.source.run_id, v)
    assert_run_id(run_id)
    v.update(run_id=run_id, tree_id=run_id, host=None, mount="", root=str(root))
    # The command runs as given; only the stage's steps, progress, budget, retry and tools come from edr.toml.
    spec = launch._spec(project, batch, job, [], [], v, stages=[replace(stage, cmd="", resume="")])
    spec["stages"][0]["cmd"] = shlex.join(argv)
    spec.pop("runtime", None)  # the tree of a tracked command is the caller's, as it is
    spec["collect"] = a.collect
    plan_ = launch.RunPlan(run_id=run_id, label=a.label, host=host, root=str(root), spec=spec, queued=False, source=source)
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
    c.db.upsert_run({"run_id": run_id, "batch": a.batch, "label": a.label, "config": a.label, "build_tag": "track",
                     "source": source, "dirty": int("-dirty" in source), "host": host, "root": str(root), "created": now,
                     "phase": "setup", "state": "running", "started": now, "tree_id": run_id,
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
    hours = a.hours if a.hours is not None or a.ack else 12
    c.emit(f"{row['run_id']}: " + _keep(c, row, hours, a.ack or None, "user") + (" (dry)" if a.dry_run else ""))
    return Exit.DONE


def cmd_import(c: Ctx, a: argparse.Namespace) -> int:
    """Record a run tree that edr did not make, or its collected results, so `reuse`, `metrics` and `export` see it."""
    assert_run_id(a.run_id)
    if not a.results and not (a.host and a.root):
        raise Refuse("import needs --host and --root, or --results DIR")
    root = (a.root or "").rstrip("/")
    if root:
        if not root.startswith("/"):
            raise Refuse(f"'{root}' is not absolute")
        rc, _, _ = c.ssh.run(a.host, ["test", "-d", root])
        if rc != 0:
            raise Refuse(f"{a.host}:{root} is not a directory")
    results = _results_dir(c, a.run_id, a.results) if a.results else None
    tasks = {t: config.resolve_task(c.project, t) for t in a.tasks or []}
    now = int(time.time())
    row = dict(run_id=a.run_id, batch=a.batch, label=a.label, config=a.config, build_tag=a.build_tag or "",
               source=a.source, dirty=0, host=a.host or "", root=root or None, created=now, phase=a.phase, state="imported",
               stage="", step=-1, exit=0 if a.phase == "done" else None, started=now, updated=now,
               counts=json.dumps({}), tree_id=a.run_id)
    where = f"{a.host}:{root}" if root else f"results {results}"
    text = f"{where} as {a.label}@{a.batch}" + (f": {a.why}" if a.why else "")
    if not a.dry_run:
        c.db.upsert_batch(dict(batch=a.batch, project=c.project.project, source=a.source, created=now))
        c.db.upsert_run(row)
        c.db.set_parameters(a.run_id, {k: row[k] for k in ("config", "build_tag", "source") if row[k]}, "import")
        if results:
            text += f", {_import_results(c, row, results, tasks)} metrics"
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


def _import_results(c: Ctx, row: Row, src: Path, tasks: dict) -> int:
    """Link `src` under data/results and extract every metric of the project from it."""
    dest = c.project.data / "results" / row["run_id"]
    if not (dest.is_symlink() or dest.exists()):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(src)
    rows = metrics.extract(c.project, row, c.project.data / "results", tasks)
    return sum(c.db.add_metric(r) for r in rows if r["value"] is not None)


def cmd_export(c: Ctx, a: argparse.Namespace) -> int:
    """Write a frozen snapshot of one source, or the project database into an MLflow store."""
    if a.mlflow:
        if a.dry_run:
            n = len([r for r in c.db.runs() if a.source is None or r.get("source") == a.source])
            c.emit(f"{a.mlflow}: {n} runs (dry)", {"runs": n})
            return Exit.DONE
        from .mlflow_export import export_mlflow

        res = export_mlflow(c.project, c.db, Path(a.mlflow), a.source)
        c.db.add_event("user", "", "export", f"mlflow {a.source or 'every source'} -> {a.mlflow}")
        c.emit(f"{res['tracking_uri']}: {len(res['written'])} runs written, {len(res['skipped'])} already there", res)
        return Exit.DONE
    if not a.source or not a.out:
        raise Refuse("export needs --source and --out, or --mlflow DIR")
    labels = a.labels.split(",") if a.labels else None
    manifest = export.export(c.project, c.db, a.source, Path(a.out), labels, a.dry_run, a.with_logs)
    if not a.dry_run:
        c.db.add_event("user", "", "export", f"{a.source} -> {a.out}")
    c.emit(f"{a.out}: {len(manifest['runs'])} runs, {len(manifest['files'])} files" + (" (dry)" if a.dry_run else ""),
           manifest)
    return Exit.DONE


def cmd_stop(c: Ctx, a: argparse.Namespace) -> int:
    """Stop one run through the driver, or the stop file with --after-task."""
    row = c.resolve(a.handle)
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
    if not a.handle and not a.batch:
        raise Refuse("retire needs a handle or --batch")
    project, dry = c.project, " (dry)" if a.dry_run else ""
    rows = c.db.runs(batch=a.batch) if a.batch else [c.resolve(a.handle)]
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
        targets = [posixpath.join(root, config.render(p, values))
                   for st in project.stages.values() for p in st.prune.get(a.prune, [])]
        if not targets:
            raise Refuse(f"no stage has prune.{a.prune}")
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
    notifiers = _notifiers(c)
    if a.check:
        return watch.check(project, notifiers)
    return watch.run_forever(project, c.ssh, c.db, notifiers, once=a.once)


def cmd_notify(c: Ctx, a: argparse.Namespace) -> int:
    """Send one message, the board or the digest through every configured notifier."""
    if (a.text is not None) + a.board + a.digest != 1:
        raise Refuse("give TEXT, --board or --digest, one of them")
    if a.board:
        title, html = "board", Actions(c).status_text()
    elif a.digest:
        title, html = "digest", Actions(c).digest_text()
    else:
        title, html = "note", tgfmt.esc(a.text)
    if a.dry_run:
        c.emit(tgfmt.head(c.project.project, title) + "\n" + html + "\n(dry)", {"sent": 0, "text": untag(html)})
        return Exit.DONE
    notifiers = make_notifiers(c.project.site, c.project, c.db, Actions(c))
    if not notifiers:
        raise Refuse("no notifier is configured; see docs/guides/alerts.md")
    sent = sum(n.post(title, html, a.silent) for n in notifiers)
    c.emit(f"sent to {sent} of {len(notifiers)} notifiers", {"sent": sent, "text": untag(html)})
    return Exit.DONE if sent == len(notifiers) else Exit.REFUSED


def _notifiers(c: Ctx) -> list:
    project = c.project
    bot = Ctx(c.a)
    bot._project = project
    # The bot polls in its own thread.
    bot._db = Database(project.data / "edr.db", threads=True)
    return make_notifiers(project.site, project, bot.db, Actions(bot))


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
HANDLE = "label@batch, a run id prefix, or #n from the last board"


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
        anywhere below the project. Without one it refuses.

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

        A handle names one run in one of three forms. label@batch is the newest
        run with that label in that batch. A run id prefix is the one run whose
        id starts with it; an ambiguous prefix is refused. The form #n is row n
        of the last board that edr status printed.

        plan and launch take the batch as an argument, which defaults to
        EDR_BATCH and then to the newest batch directory in the state. status
        --batch defaults to EDR_BATCH and then to every batch. retire needs a
        handle or --batch.
        """))
    p.add_argument("--json", action="store_true", help="print the result as JSON")
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
        command, and the last ten events. It ends with the project's CLAUDE.md
        and AGENTS.md, when they exist, and the documentation.

        With --run, it prints the history of one run instead: its identity,
        its stage and step times from stage_runs and step_runs, its events with
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
        number, label, host, state, phase, stage/step, heartbeat age, failed and
        done task counts, and the core hours so far. The state of a live run
        follows the heartbeat age (running, stale, dead) or the watcher's last
        verdict (hung, host_full, ...). A finished run shows its phase class:
        done, incomplete, failed, over_budget, stopped or killed.

        With a handle, it prints one run: its identity, state and disk, every
        stage and task row, the CPU, RSS, tree size and free disk the driver sampled over the
        run, the metrics, and the log tail from the heartbeat. A finished run
        shows driver exit <n> (<phase>): the code of the driver, whose phase
        names the stage that failed. The command exit column of the stage table
        is the code of the stage command itself. The tasks line with the done
        and failed counts appears only for a run with a task group.
        """, exits={Exit.HOSTS: "with --live, a host did not answer"})
    s.add_argument("handle", nargs="?", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="one batch; default EDR_BATCH, else every batch")
    s.add_argument("--narrow", action="store_true", help="48 columns, two lines per live run, for an ssh app on a phone")
    s.add_argument("--watch", action="store_true", help="redraw every heartbeat_s seconds; Ctrl-C ends it")
    s.add_argument("--live", action="store_true", help="ask each host whether the driver exists; a gone driver shows dead")
    s.add_argument("--triage", action="store_true", help="every run not running, with one proposed command")
    s.add_argument("--digest", action="store_true", help="the daily digest that the watcher sends, as plain text")
    s = command("events", "the last events", """
        Prints the last N events, oldest first, with the time, the actor (user,
        watch or telegram), the run, the kind and the text.
        """, exits={Exit.NOTHING: "no event"})
    s.add_argument("--since", metavar="T", help="only events newer than this: 30m, 2h, 1d or seconds")
    s.add_argument("--run", metavar="HANDLE", help="the events of one run")
    s.add_argument("-n", type=int, default=50, help="the last N events; default 50")
    s = command("hosts", "probe every host", """
        Probes every host of the site file and prints one row per host: the
        worst mark of the host; the name; cores in use of total, with a bar; the
        one-minute load average; RAM free of total; the largest writable scratch
        of the host's list, and its space free of total, with a bar of the used
        part; GPUs idle of total, where idle means under 5 % utilisation and
        under 5 % memory in use; GPU memory free of total, summed over the GPUs;
        processes that match tool_procs, split into yours and other users';
        and the live runs of this project on the host, from the database.

        A mark tells how full a resource is. It is 🟢 below the first threshold
        of the [marks] table, 🟡 from the first, 🟠 from the second and 🔴 from
        the third. A value exactly at a threshold takes the colour of that
        threshold. The used fraction is the load over the cores for cores, the
        used part of the total for ram GB and scratch GB, and the busy GPUs over
        all GPUs for gpu. A host without GPUs shows -, and a host that did not
        answer shows ⚫ and its error in the row. The rows go by the worst mark,
        ⚫ first, then 🔴, 🟠, 🟡 and 🟢, and by host name within one mark.

        The GPU columns come from nvidia-smi; a host without it shows -. A bar is
        green below 70 % used, yellow below 90 % and red above; unlike the
        marks, the bar colour does not follow [marks]. --json gives the
        numbers: cores, load, free_cores, free_ram_gb, total_ram_gb, mount,
        free_gb, total_gb, gpus, gpus_idle, gpu_used_gb, gpu_total_gb,
        our_tool_procs, other_tool_procs and our_runs, and the marks of cores,
        ram, scratch and gpu in marks.

        --history probes nothing. It reads the host_samples table, where the
        watcher keeps one probe per host and cycle for 30 days, and prints one
        row per host: the first and last sample, and a line each of cores in
        use (the load, capped at the cores), RAM, scratch and busy GPUs over
        --since, each with its peak and its last value.
        """, exits={Exit.HOSTS: "a host did not answer", Exit.NOTHING: "with --history, no sample"})
    s.add_argument("--history", action="store_true",
                   help="no probe: the samples the watcher kept, one line per host over --since")
    s.add_argument("--since", default="1d", metavar="T", help="with --history: 30m, 2h, 1d or seconds; default 1d")
    s.add_argument("--narrow", action="store_true",
                   help="only the mark (column ok), host, cores, RAM, scratch and GPUs, in 48 columns, "
                        "with no space between a mark and its number")
    command("tools", "every site tool: free seats and hosts", """
        Prints one row per tool of the site file. free and total are the seats the
        probe reports; the probe runs on the head node with the project
        directory as {root}. hosts lists the hosts that have the tool, with
        their versions. A tool without a probe shows - for the seats. --json
        gives tool, free, total, hosts (host to version) and note.
        """, exits={Exit.HOSTS: "a probe failed, or printed no number"})
    s = command("metrics", "the metrics of one source or one run", """
        Prints every metric of one source with its label, source, stage, step,
        task, name, value and unit. --source or --run is required. --source
        is the source tag exactly as edr checkout printed it, -dirty-...
        included; --run takes one run instead. --csv writes the columns of
        metrics.csv (docs/guides/results.md) to stdout.

        --run with --over steps prints the metrics along the steps of that run:
        one row per step with its name, one column per metric. With --metric,
        it prints that one metric, its change from the step before and its
        source file.

        --instance or --depth prints the area rows of an area_hier metric
        instead: label, source, stage, step, instance, depth, area with the
        children, local area without them, and the cell count when the report
        has one. --instance takes that instance and every instance below it.
        """, exits={Exit.NOTHING: "no metric row"})
    s.add_argument("--source", metavar="SOURCE", help="the exact source tag of the runs, as in the run id")
    s.add_argument("--run", metavar="HANDLE", help="one run: " + HANDLE)
    s.add_argument("--metric", metavar="NAME", help="one metric, by name or canonical name")
    s.add_argument("--over", choices=["steps"], help="with --run: the metrics along the steps")
    s.add_argument("--stage", metavar="S", help="the metrics of one stage")
    s.add_argument("--step", type=int, metavar="N", help="the metrics of one step number")
    s.add_argument("--csv", action="store_true", help="CSV on stdout")
    s.add_argument("--instance", metavar="PATH", help="the area rows of this instance and every instance below it")
    s.add_argument("--depth", type=int, metavar="N", help="the area rows at this depth; the top is 0")
    s = command("extract", "extract the metrics of runs again from their collected files", """
        Extracts every metric in edr.toml again from the files collected for
        each run under data/results. It uses the same function as the watcher,
        so it reads the tasks that finished and the stages that ended done. A
        run without a heartbeat, such as an imported one, is read for every
        stage. New rows are added. A row is replaced when its value, canonical
        name or unit has changed, or when its area_hier metric has no area
        rows yet. Rows that the new extraction does not find are kept.

        Pass exactly one of a handle, --batch or --source. For each run,
        extract prints how many rows are new, changed, unchanged and failed,
        where a failed row is a file that did not parse, and it writes an
        extract event with the same counts. With --json, data holds run_id,
        new, changed, unchanged and failed for each run.
        """, write=True, exits={Exit.NOTHING: "no run matches"})
    s.add_argument("handle", nargs="?", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="every run of the batch")
    s.add_argument("--source", metavar="SOURCE", help="every run of the exact source tag")
    s = command("compare", "two or more runs side by side", """
        Puts two or more runs side by side. Without --area, it prints one row
        per stage, step, task and metric: the step name, the value of each run, and the
        percent of each run to the first. --metric (repeatable), --stage and
        --step narrow the rows; --json keeps the source file of every value.

        --area compares the hierarchical area: one row per instance at --depth (default 1; the top is 0), one
        column per run, and the delta and the percent of each run to the
        first. Each run is compared at its last step with an area report, or
        at --stage and --step. The source file of each run is printed under
        the table.
        """, exits={Exit.NOTHING: "no row to compare"})
    s.add_argument("handles", nargs="+", metavar="HANDLE", help=HANDLE)
    s.add_argument("--area", action="store_true", help="the hierarchical area per instance")
    s.add_argument("--metric", action="append", metavar="NAME", help="this metric, by name or canonical name; repeatable")
    s.add_argument("--depth", type=int, default=1, metavar="N", help="the instance depth; default 1")
    s.add_argument("--instance", metavar="PATH", help="only this instance and the instances below it")
    s.add_argument("--stage", metavar="S", help="this stage only; with --area, compare at this stage")
    s.add_argument("--step", type=int, metavar="N", help="this step only; with --area, compare at this step number")
    s = command("runtime", "stage, step and task times", """
        With one handle, runtime prints the times of one run: a row per stage
        attempt from
        the stage_runs table, a row per step under it, and one row per task
        group with the task count, the summed task time and the longest task.
        A step starts when the driver first sees its number, or at the time
        the stage's step_log finds in a collected file; it ends when the next
        step starts or the stage ends. The source column names the table or
        the file and line of each time. The total sums the stage attempts.

        With several handles or --batch, it prints one row per run: the wall
        time of each stage, with the attempts summed, and the total.
        """, exits={Exit.NOTHING: "no stage or step time"})
    s.add_argument("handles", nargs="*", metavar="HANDLE", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="every run of the batch")
    command("init", "write edr.toml and the watch unit here", """
        Writes edr.toml and edr-watch.service into the current directory, the
        project directory where you run edr. --site names the site directory
        or the site file itself; init does not write that file. It refuses
        when edr.toml already exists.
        """, write=True).add_argument("--site", required=True, metavar="DIR", help="the site directory, or a site.toml path")
    command("check", "load everything, probe the hosts, check the hooks", """
        Loads the project, the site and every batch under jobs/, imports every
        hook, checks the driver file, probes every host once, names every tool
        the head node lacks, and plans every batch with those probes. It prints
        one problem: line per fault, or an ok: line with the counts.
        """, exits={Exit.REFUSED: "a problem was found"})
    s = command("checkout", "check out a ref as a clone, or a dirty tree as a snapshot", """
        Fetches the repository, then makes a detached local clone of ref (default source.ref) at
        <worktrees>/<short hash>, and clones each source.nested repository into
        it at the HEAD the repository copy has. A local clone shares the git
        objects of the repository by hard links. It prints <source> <path>.

        --dirty DIR clones the HEAD of a working tree and copies its files over
        the clone, with the diff in source.diff; the tag is <hash>-dirty-<8 hex>
        and is printed with (dirty). A clean tree under --dirty is checked out as a
        clone.
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
        Runs one more stage on the tree of an existing run, as a new run in the
        batch of that run with the label <label>.<stage>.
        --tasks names the tasks of a task group, --parallel its width, --on the
        host (default: the tree's host). --from fills {checkpoint} in the
        stage's resume command, and is refused when the stage has none.

        --collect NAME instead copies the collect_on_request list NAME of every
        stage from the tree into data/results/<run id>/.
        """, write=True, exits={Exit.REFUSED: "the plan has a problem, or --from names a stage without resume",
                                Exit.HOSTS: "with --collect, a copy failed"})
    s.add_argument("handle", help=HANDLE)
    s.add_argument("--stage", metavar="S", help="the stage to run on the tree")
    s.add_argument("--tasks", nargs="+", metavar="ID", help="the tasks of a task group; default the job's")
    s.add_argument("--from", dest="from_", metavar="CHECKPOINT", help="resume the stage from this checkpoint")
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
        run id follows source.run_id with the label as config, track as the
        build tag, and --source (default the source tag of the tree) as {source}.

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
    s.add_argument("--source", metavar="SOURCE", help="the source tag; default the tag of the tree")
    s.add_argument("--root", metavar="DIR", help="the run tree; default the current directory")
    s.add_argument("--collect", action="store_true", help="the watcher collects the stage and extracts its metrics")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
    s = command("keep", "add hours to the running stage or task; --ack cancels a pending kill", """
        Writes the keep file of a live run. --hours (default 12 when --ack is
        absent) adds hours to the budget of the running stage or task; --ack
        cancels a pending kill or stop of the watcher.
        """, write=True, exits={Exit.NOTHING: "the run has ended"})
    s.add_argument("handle", help=HANDLE)
    s.add_argument("--hours", type=int, metavar="N",
                   help="hours to add to the budget of the running stage or task; default 12 without --ack")
    s.add_argument("--ack", action="store_true", help="cancel the watcher's pending kill or stop")
    s = command("import", "record a run tree that edr did not make, or its collected results", """
        Records a run that edr did not start, such as one you ran by hand. With
        --host and --root, it records the tree on that host, so reuse and edr
        continue can build on it. --results DIR names a directory of collected
        files from a run whose tree is gone; it is linked as
        data/results/<run id>, and the project's metrics are extracted from it; --tasks names the tasks whose files it holds. The run id must start
        with YYYYMMDD_HHMM_.
        """, write=True)
    s.add_argument("--run-id", required=True, dest="run_id", help="the run id; it must start with YYYYMMDD_HHMM_")
    s.add_argument("--label", required=True, help="the label of the run")
    s.add_argument("--config", default="", help="the configuration name of the run; default empty")
    s.add_argument("--source", required=True, metavar="SOURCE", help="the source tag of the tree")
    s.add_argument("--host", help="the host of the tree")
    s.add_argument("--root", metavar="PATH", help="the tree on the host")
    s.add_argument("--results", metavar="DIR", help="collected files in the run layout; linked as data/results/<run id>")
    s.add_argument("--tasks", nargs="+", metavar="ID", help="the tasks whose files the results hold")
    s.add_argument("--batch", default="imported", help="the batch to record it in; default imported")
    s.add_argument("--phase", default="done", help="the terminal phase; default done")
    s.add_argument("--build-tag", dest="build_tag", metavar="TAG", help="the build tag of the run")
    s.add_argument("--why", default="", help="the reason; it goes into the events table")
    s = command("export", "a frozen snapshot of one source", """
        Writes a snapshot of one source to DIR: manifest.json, runs.csv,
        metrics.csv and the collected files of the newest run per label.
        --source matches the source tag exactly. log/ and *.log stay out unless
        you pass --with-logs. It refuses a DIR that exists and is not empty.
        docs/guides/results.md explains the layout.

        --mlflow DIR writes the project database into a local MLflow tracking store
        in DIR instead (mlflow.db and artifacts/), for mlflow ui: one MLflow run
        per run, of every source or of --source, with the parameters, the
        metrics at their step, the stage and step times, and the collected
        files up to 1 MiB. A run already in the store is skipped. It needs the
        mlflow extra: pip install 'edarunner[mlflow]'.
        """, write=True)
    s.add_argument("--source", metavar="SOURCE", help="the exact source tag of the runs, as in the run id")
    s.add_argument("--out", metavar="DIR", help="the directory to write; it must be absent or empty")
    s.add_argument("--mlflow", metavar="DIR", help="write an MLflow tracking store in DIR instead")
    s.add_argument("--labels", metavar="a,b", help="these labels only, comma separated")
    s.add_argument("--with-logs", dest="with_logs", action="store_true", help="also copy log/ directories and *.log files")
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
        prune.T names in the stages, after the guard on every target. --batch
        retires every run of the batch and marks it RETIRED, so the watcher
        skips it. A live run gets the phase ABANDONED:<why>.

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
        """, write=True, why=True, exits={Exit.NOTHING: "the batch has no run", Exit.HOSTS: "an rm failed"})
    s.add_argument("handle", nargs="?", help=HANDLE)
    s.add_argument("--batch", metavar="B", help="every run of the batch, then mark it RETIRED")
    s.add_argument("--collect", metavar="NAMES", help="copy these collect_on_request lists, comma separated, to the head node first")
    s.add_argument("--prune", metavar="T", help="remove the prune targets named T instead of the tree")
    s.add_argument("--uncollected", action="store_true", help="remove a tree whose results were never collected")
    s = command("notify", "send one message, the board or the digest through every notifier", """
        Sends one message through every notifier that the site configures. The
        message starts with a header line with the project name, like every
        message of the bot, and TEXT follows as plain text. --board sends the
        board of edr status and --digest the daily digest instead, so a cron
        line can mail either.
        """, write=True, exits={Exit.REFUSED: "no notifier is configured, a send failed, or not exactly one "
                                              "of TEXT, --board and --digest"})
    s.add_argument("text", nargs="?", help="the message, as plain text")
    s.add_argument("--board", action="store_true", help="send the board of edr status")
    s.add_argument("--digest", action="store_true", help="send the daily digest now; the watcher still sends its own")
    s.add_argument("--silent", action="store_true", help="send without a sound on the phone")
    s = command("watch", "the watcher", """
        Runs the watcher loop: one cycle every heartbeat_s seconds, with the
        Telegram bot as a thread when the site file configures it. --once runs one cycle.
        --check reads the watcher's own heartbeat; a cron line runs it. --dry-run
        reads and classifies every run, prints the states and writes nothing.
        docs/how-it-works.md explains the cycle.
        """, write=True, exits={Exit.REFUSED: "with --once, the cycle failed or the config did not load; "
                                              "with --check, watch.json is older than three cycles"})
    s.add_argument("--once", action="store_true", help="one cycle; exit 1 when it failed")
    s.add_argument("--check", action="store_true", help="exit 1 when watch.json is older than three cycles")
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

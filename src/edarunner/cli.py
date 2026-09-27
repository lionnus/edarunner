"""The `edr` command: seventeen verbs.

Every verb wires the modules; nothing here knows a file format. Exit
codes: 0 done, 1 refused or bad input, 2 nothing to do, 3 some hosts
failed. `--json` prints {"code", "data", "output"} with the captured text.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import functools
import getpass
import io
import json
import logging
import os
import posixpath
import shlex
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from string import Template
from typing import Any

from . import __version__, board, collect, config, export, launch, metrics, runid, stagectl, sync, watch
from .config import ConfigError
from .guards import Refuse, assert_run_id, assert_safe_target
from .hosts import HostError, HostProbe, Ssh
from .ledger import Ledger
from .model import Batch, Job, Project
from .notify import make_notifiers

TEMPLATES = Path(__file__).resolve().parent / "templates"
Row = dict[str, Any]
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_STOP_FLAGS = {"hung": "--why hung", "looping": "--why looping", "over_budget": "--why over-budget",
               "host_full": "--now --why host-full", "superseded": "--after-task --why superseded"}


# --- helpers

def _since(text: str) -> int:
    """'30m', '2h', '1d' or seconds, as the unix time that long ago."""
    try:
        unit = _UNIT.get(text[-1])
        secs = float(text[:-1]) * unit if unit else float(text)
    except (ValueError, IndexError):
        raise Refuse(f"--since {text!r}: use 30m, 2h, 1d or seconds") from None
    return int(time.time() - secs)


_READ_VERBS = frozenset({"status", "events", "hosts", "lic", "metrics", "check"})


class Ctx:
    """Lazy project, ledger and ssh of one invocation, plus the payload of --json."""

    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.data: Any = None
        self._project: Project | None = None
        self._ledger: Ledger | None = None

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
    def ledger(self) -> Ledger:
        if self._ledger is None:
            path = self.project.data / "edr.db"
            # A read verb or a dry run creates nothing, not even an empty database.
            memory = not path.exists() and (self.a.dry_run or self.a.verb in _READ_VERBS)
            self._ledger = Ledger(":memory:") if memory else Ledger(path)
        return self._ledger

    @functools.cached_property
    def ssh(self) -> Ssh:
        return Ssh(self.project.site)

    def close(self) -> None:
        """Close the ledger when a verb opened it."""
        if self._ledger is not None:
            self._ledger.close()

    def emit(self, text: str, data: Any = None) -> None:
        """Print `text`; keep `data` (default: the text) for --json."""
        self.data = text if data is None else data
        if not self.a.json:
            print(text)

    def batch_name(self, name: str | None) -> str:
        """`name`, else EDR_BATCH, else the newest batch directory of the state."""
        name = name or os.environ.get("EDR_BATCH")
        if name:
            return name
        dirs = [p for p in self.project.state.glob("*") if p.is_dir() and p.name != "bin"]
        if not dirs:
            raise Refuse(f"no batch given and no batch directory in {self.project.state}")
        return max(dirs, key=lambda p: p.stat().st_mtime).name

    def batch(self, name: str | None) -> Batch:
        """Load a batch with its source resolved."""
        return self.resolve_source(config.load_batch(self.project, self.batch_name(name)))

    def resolve_source(self, b: Batch) -> Batch:
        """A ref as source becomes the src tag of its staged tree; StageError when it is not staged."""
        if not stagectl.SRC_RE.match(b.source):
            b.source = runid.src_tag(stagectl.find(self.project, b.source))
        return b

    def refresh(self, batch: str | None = None) -> None:
        """Ingest the heartbeat files of the shown batches, so the board follows the driver, not the last watcher cycle.

        This is the watcher's own first step and idempotent, so a read verb may run it.
        """
        heartbeats = watch.read_heartbeats(self.project, {batch} if batch else None)
        if heartbeats:
            watch.ingest(self.ledger, heartbeats)

    def rows(self, batch: str | None = None) -> list[Row]:
        """The runs of the batches that are not retired, with the state a heartbeat age gives."""
        self.refresh(batch)
        retired = {b["batch"] for b in self.ledger.batches() if b.get("retired")}
        rows = [r for r in self.ledger.runs(batch=batch) if r["batch"] not in retired]
        now, lim = time.time(), self.project.limits
        for r in rows:
            # The watcher's own classes (hung, host_full, ...) stay; only the age classes follow the heartbeat.
            if board.is_live(r) and r.get("updated") is not None and r.get("state") in (None, "running", "stale", "dead"):
                age = now - r["updated"]
                r["state"] = "dead" if age > lim.dead_s else "stale" if age > lim.stale_s else "running"
        return rows

    def resolve(self, handle: str) -> Row:
        """The ledger row of label@batch, a run id prefix, or #n from the last board."""
        try:
            run_id = self.ledger.resolve(handle, self.ledger.get_kv("last_board"))
        except KeyError as e:
            raise Refuse(str(e.args[0])) from None
        row = self.ledger.run(run_id)
        if row is None:
            raise Refuse(f"{handle}: no run {run_id}")
        return row

    def heartbeat(self, row: Row) -> dict:
        """The heartbeat of a run, or {} when the driver wrote none."""
        return config.load_json(self.project.state / str(row["batch"]) / f"{row['run_id']}.json")

    def save_board(self, rows: list[Row]) -> None:
        """Keep the board order in the ledger, so #n resolves next time."""
        if rows:
            self.ledger.set_kv("last_board", [r["run_id"] for r in board.order(rows)])


# --- texts shared by the verbs and the bot

def _events_text(c: Ctx, events: list[Row]) -> str:
    names = {r["run_id"]: f"{r['label']}@{r['batch']}" for r in c.ledger.runs()}
    return "\n".join(
        f"{time.strftime('%m-%d %H:%M', time.localtime(e['ts']))} {e['actor']:<8} "
        f"{names.get(e['run_id'], e['run_id'] or '-')} {e['kind']}: {e['text']}" for e in events) or "no events"


def _probe_rows(c: Ctx) -> list[Row]:
    out: list[Row] = []
    for host in c.project.site.hosts:
        try:
            out.append(asdict(c.ssh.probe(host)))
        except HostError as e:
            out.append({"host": host, "error": str(e)})
    return out


def _hosts_text(rows: list[Row], narrow: bool) -> str:
    head = ["host", "cores", "ram_gb", "free_gb"] + ([] if narrow else ["mount", "tools", "runs"])
    body = []
    for r in rows:
        if "error" in r:
            body.append([r["host"], "error: " + r["error"]])
            continue
        body.append([r["host"], r["free_cores"], r["free_ram_gb"], r["free_gb"]] + ([] if narrow else [
            r["mount"], f"{r['our_tool_procs']}/{r['other_tool_procs']}", r["our_runs"]]))
    return board.table(head, body)


def _lic_rows(c: Ctx) -> list[Row]:
    project, me = c.project, getpass.getuser()
    # A probe says {root}; from the head node the project directory stands in for it.
    values = config.placeholders(project, root=str(project.root), host="local")
    out: list[Row] = []
    for name, lic in project.site.licences.items():
        row: Row = {"licence": name, "feature": lic.feature, "floor": lic.floor}
        try:
            rc, text, err = c.ssh.run("local", config.render(lic.probe, values))
        except ConfigError as e:
            rc, text, err = 1, "", str(e)
        parsed = metrics.parse_flexlm(text, lic.feature) if rc == 0 else None
        if parsed is None:
            row["note"] = "unknown: " + (err.strip() or "no feature line").splitlines()[0]
        else:
            issued, used = parsed
            block = text.split(f"Users of {lic.feature}:", 1)[1].split("Users of ", 1)[0]
            ours = sum(1 for ln in block.splitlines() if ln.split()[:1] == [me])
            row.update(pool=issued, used=used, free=issued - used, ours=ours, others=used - ours)
        out.append(row)
    return out


def _lic_text(rows: list[Row]) -> str:
    keys = ["licence", "feature", "pool", "used", "free", "ours", "others", "floor", "note"]
    return board.table(keys, [[r.get(k, "") for k in keys] for r in rows]) if rows else "no licences"


def _metric_key(m: Row) -> str:
    return (m.get("canonical") or m["name"]) + (f"[{m['task']}]" if m.get("task") else "")


def _compare_text(c: Ctx, handles: list[str]) -> str:
    rows = [c.resolve(h) for h in handles]
    final: dict[str, dict[str, tuple[int, Any]]] = {}
    for m in c.ledger.metrics(run_ids=[r["run_id"] for r in rows]):
        cur, step = final.setdefault(_metric_key(m), {}), -1 if m.get("step") is None else int(m["step"])
        if step >= cur.get(m["run_id"], (-2, None))[0]:
            cur[m["run_id"]] = (step, m["value"])
    body = [[k, *[cur.get(r["run_id"], (0, None))[1] for r in rows]] for k, cur in sorted(final.items())]
    return board.table(["metric", *[str(r["label"]) for r in rows]], body) if body else "no metrics"


def _metrics_text(rows: list[Row]) -> str:
    body = [[m.get("label"), m.get("src"), m["stage"], m.get("step"), m.get("task") or "", m["name"], m["value"],
             m.get("unit")] for m in rows]
    return board.table(["label", "design", "stage", "step", "task", "metric", "value", "unit"], body) if rows else "no metrics"


def _keep(c: Ctx, row: Row, hours: int | None, ack: bool | None, actor: str) -> str:
    """Write the keep file next to the spec; a field not given keeps its current value."""
    run_id = row["run_id"]
    path = c.project.state / str(row["batch"]) / f"{run_id}.keep.json"
    cur = config.load_json(path)
    data = {"hours": cur.get("hours", 0) if hours is None else hours,
            "ack": bool(cur.get("ack")) if ack is None else ack}
    note = f"keep {data['hours']} h" + (", ack" if data["ack"] else "")
    if not c.a.dry_run:
        config.save_json(path, data)
        c.ledger.add_event(actor, run_id, "keep", note)
    return f"{run_id}: {note}"


class Actions:
    """The verbs the Telegram bot may call; each one is a CLI verb without the printing."""

    def __init__(self, c: Ctx) -> None:
        self.c = c

    def keep(self, handle: str, hours: int, actor: str) -> str:
        return _keep(self.c, self.c.resolve(handle), hours, None, actor)

    def ack(self, handle: str, actor: str) -> str:
        return _keep(self.c, self.c.resolve(handle), None, True, actor)

    def stop_after_task(self, handle: str, actor: str, why: str) -> str:
        c, row = self.c, self.c.resolve(handle)
        if not board.is_live(row):
            return f"{row['run_id']} already {row['phase']}"
        launch.stop(c.ssh, c.ledger, row, {}, after_task=True, why=why, state=c.project.state, actor=actor)
        return f"{row['run_id']} stops after its task"

    def status_text(self, narrow: bool = True) -> str:
        rows = self.c.rows()
        self.c.save_board(rows)
        return board.narrow(rows) if narrow else board.wide(rows)

    def events_text(self, n: int) -> str:
        return _events_text(self.c, self.c.ledger.events(n=n))

    def hosts_text(self) -> str:
        return _hosts_text(_probe_rows(self.c), narrow=True)

    def lic_text(self) -> str:
        return _lic_text(_lic_rows(self.c))

    def compare_text(self, handles: list[str]) -> str:
        return _compare_text(self.c, handles)

    def metric_text(self, name: str, design: str | None) -> str:
        return _metrics_text(self.c.ledger.metrics(design=design, name=name))


# --- verbs

def cmd_status(c: Ctx, a: argparse.Namespace) -> int:
    """The board, or one run with its stages, metrics and log tail."""
    if a.handle:
        row = c.resolve(a.handle)
        c.refresh(str(row["batch"]))
        row = c.ledger.run(row["run_id"]) or row
        hb, run_id = c.heartbeat(row), row["run_id"]
        stages = [dict(r) for r in c.ledger.db.execute(
            "SELECT * FROM stage_runs WHERE run_id=? ORDER BY stage, task, attempt", (run_id,))]
        mets = c.ledger.metrics(run_ids=[run_id])
        c.emit(board.run_detail(row, stages, mets, str(hb.get("last_log") or "")),
               {"run": row, "heartbeat": hb, "stages": stages, "metrics": mets})
        return 0
    code = 0
    while True:
        rows = c.rows(a.batch or os.environ.get("EDR_BATCH"))
        if a.live:
            code = max(code, _mark_live(c, rows))
        c.save_board(rows)
        text = _triage(c, rows) if a.triage else board.narrow(rows) if a.narrow else board.wide(rows)
        if a.watch and not a.json:
            print("\x1b[2J\x1b[H", end="")
        c.emit(text, {"runs": rows})
        if not a.watch or a.json:
            return code
        time.sleep(c.project.limits.heartbeat_s)


def _mark_live(c: Ctx, rows: list[Row]) -> int:
    """Ask each host whether the driver of a live run exists; a gone driver marks the run dead."""
    code = 0
    for r in rows:
        pid = c.heartbeat(r).get("driver_pid") if board.is_live(r) else None
        if not pid or not r.get("host"):
            continue
        try:
            r["alive"] = c.ssh.pid_alive(str(r["host"]), int(pid))
        except HostError:
            r["alive"], code = None, 3
        if r["alive"] is False:
            r["state"] = "dead"
    return code


def _triage(c: Ctx, rows: list[Row]) -> str:
    lines = []
    for r in board.order(rows):
        state, h = board.state_of(r), f"{r['label']}@{r['batch']}"
        if state == "running":
            continue
        if state == "queued":
            cmd = f"edr launch {r['batch']} --only {r['label']}"
        elif state == "stale":
            cmd = f"edr status {h} --live"
        elif state == "dead":
            hb = c.heartbeat(r)
            cmd = f"edr run {h} --stage {hb.get('stage') or r.get('stage')}" + (
                f" --from {hb['step_name']}" if hb.get("step_name") else "")
        elif state in _STOP_FLAGS:
            cmd = f"edr stop {h} {_STOP_FLAGS[state]}"
        elif state == "done":
            cmd = f"edr export --design {r.get('src')} --out exports/{r.get('src')}"
        else:
            cmd = f"edr retire {h} --why {state}"
        lines.append(f"{state:<11} {h:<28} {r.get('phase') or '-'}\n    {cmd}")
    return "\n".join(lines) or "nothing to triage"


def cmd_events(c: Ctx, a: argparse.Namespace) -> int:
    """The last events, filtered by time and run."""
    since = _since(a.since) if a.since else None
    run_id = c.resolve(a.run)["run_id"] if a.run else None
    events = c.ledger.events(since_s=since, run_id=run_id, n=a.n)
    c.emit(_events_text(c, events), events)
    return 0 if events else 2


def cmd_hosts(c: Ctx, a: argparse.Namespace) -> int:
    """Probe every site host."""
    rows = _probe_rows(c)
    c.emit(_hosts_text(rows, a.narrow), rows)
    return 3 if any("error" in r for r in rows) else 0


def cmd_lic(c: Ctx, a: argparse.Namespace) -> int:
    """Probe every site licence from the head node."""
    rows = _lic_rows(c)
    c.emit(_lic_text(rows), rows)
    return 3 if any("note" in r for r in rows) else 0


def cmd_metrics(c: Ctx, a: argparse.Namespace) -> int:
    """The metrics of one design, as a table or CSV."""
    rows = c.ledger.metrics(design=a.design, stage=a.stage, step=a.step)
    if a.csv and not a.json:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(export.METRIC_COLUMNS)
        w.writerows([m["run_id"], m.get("label"), m.get("config"), m.get("src"), m["stage"], m.get("step"),
                     m.get("task"), m["name"], m.get("canonical"), m["value"], m.get("unit"), m.get("source_file")]
                    for m in rows)
        c.data = rows
    else:
        c.emit(_metrics_text(rows), rows)
    return 0 if rows else 2


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
    return 0


def cmd_check(c: Ctx, a: argparse.Namespace) -> int:
    """Load every file, probe the hosts, import the hooks, plan every batch."""
    problems: list[str] = []
    try:
        project = c.project
    except ConfigError as e:
        c.emit(f"problem: {e}", {"problems": [str(e)]})
        return 1
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
        # The same src tag as plan; check runs before stage, so a ref that is not staged stays as written.
        with contextlib.suppress(stagectl.StageError):
            c.resolve_source(b)
        batches.append(b)
    hosts = _probe_rows(c)
    problems += [f"{r['host']}: {r['error']}" for r in hosts if "error" in r]
    problems += [p for p in c.ssh.check_local() if p not in problems]
    probes = {r["host"]: HostProbe(**r) for r in hosts if "error" not in r}
    for b in batches:
        bad = [f"{b.batch}: job {j.label} names unknown host {j.host}" for j in b.jobs
               if j.host != "auto" and j.host not in project.site.hosts]
        problems += bad
        if not bad:
            for p in launch.plan(project, b, c.ssh, c.ledger, probes=probes):
                problems += [f"{b.batch}/{p.label}: {x}" for x in p.problems]
    text = "\n".join(f"problem: {p}" for p in problems) or (
        f"ok: {len(hosts)} hosts, {len(project.stages)} stages, {len(project.metrics)} metrics, {len(batches)} batches")
    c.emit(text, {"problems": problems, "hosts": hosts, "batches": [b.batch for b in batches]})
    return 1 if problems else 0


def cmd_stage(c: Ctx, a: argparse.Namespace) -> int:
    """Stage a ref as a worktree, or a dirty tree as a snapshot."""
    res = stagectl.stage(c.project, a.ref, Path(a.dirty) if a.dirty else None, a.dry_run)
    c.emit(f"{res.src} {res.path}" + (" (dirty)" if res.dirty else ""),
           {"src": res.src, "path": str(res.path), "nested": res.nested, "dirty": res.dirty})
    return 0


def cmd_plan(c: Ctx, a: argparse.Namespace) -> int:
    """Render every job of a batch; writes nothing."""
    plans = launch.plan(c.project, c.batch(a.batch), c.ssh, c.ledger)
    c.emit("\n".join(f"{p.run_id}: {p.host or 'queued'} {p.root}" + "".join(f"\n    problem: {x}" for x in p.problems)
                     for p in plans),
           [{"run_id": p.run_id, "label": p.label, "host": p.host, "root": p.root, "queued": p.queued,
             "problems": p.problems, "spec": p.spec} for p in plans])
    return 1 if any(p.problems for p in plans) else 0


def cmd_launch(c: Ctx, a: argparse.Namespace) -> int:
    """Start one driver per job of a batch."""
    only = a.only.split(",") if a.only else None
    rows = launch.launch(c.project, c.batch(a.batch), c.ssh, c.ledger, dry_run=a.dry_run, only=only,
                         allow_dirty=a.allow_dirty)
    started, queued = sum(r["started"] for r in rows), sum(r["queued"] for r in rows)
    problems = [r["problems"] for r in rows if r["problems"]]
    c.emit(f"{started} started, {queued} queued, {len(problems)} with problems" + (" (dry)" if a.dry_run else ""), rows)
    if started or queued or (a.dry_run and not problems):
        return 0
    # A second launch of the same batch names every old job "already launched"; that is nothing to do.
    return 2 if all(any(p.startswith("already launched") for p in ps) for ps in problems) else 1


def cmd_run(c: Ctx, a: argparse.Namespace) -> int:
    """Run one stage on the tree of an existing run, or fetch a collect_on_request list."""
    row = c.resolve(a.handle)
    project, run_id = c.project, row["run_id"]
    if a.collect:
        tasks = list(c.heartbeat(row).get("tasks") or [])
        res = collect.collect_on_request(project, c.ssh, c.ledger, {**row, "tasks": tasks}, a.collect, a.dry_run)
        if not a.dry_run:
            c.ledger.add_event("user", run_id, "collect", f"{a.collect}: {res.files} files, {len(res.failures)} failed")
        c.emit("\n".join([f"{run_id}: {res.files} files" + (" (dry)" if a.dry_run else ""), *res.failures]), asdict(res))
        return 3 if res.failures else 0
    if not a.stage:
        raise Refuse("run needs --stage or --collect")
    if a.stage not in project.stages:
        raise Refuse(f"unknown stage {a.stage}")
    try:
        batch = config.load_batch(project, str(row["batch"]))
        job = next((j for j in batch.jobs if j.label == row["label"]), None)
    except (ConfigError, OSError):
        # An imported tree has no jobs file; the ledger row is the job.
        batch, job = None, None
    if job is None:
        if not row.get("config"):
            raise Refuse(f"{run_id}: no job {row['label']} in jobs/{row['batch']}.toml and no config in the ledger")
        job = Job(label=str(row["label"]), config=str(row["config"]))
        batch = Batch(batch=str(row["batch"]), source=str(row["src"] or ""), jobs=[job],
                      path=project.root / "jobs" / f"{row['batch']}.toml")
    job.reuse, job.stages, job.host = {"run_id": run_id}, [a.stage], a.on or "auto"
    if a.tasks:
        job.tasks = a.tasks
    if a.parallel:
        project.stages[a.stage].parallel = a.parallel
    # The run joins the batch of the tree it continues; the stage in the label and the time keep its id apart.
    job.label = f"{job.label}.{a.stage}"
    batch.batch, batch.jobs = str(row["batch"]), [job]
    state = project.state
    (p,) = launch.plan(project, batch, c.ssh, c.ledger, date=time.strftime(launch.DATE_FMT))
    if p.problems:
        c.emit("\n".join(f"{p.run_id}: problem: {x}" for x in p.problems), {"problems": p.problems})
        return 1
    if a.from_:
        if not p.spec["stages"][0].get("resume"):
            raise Refuse(f"stage {a.stage} has no resume command; --from needs one")
        p.spec["start_at"]["checkpoint"] = a.from_
    driver = sync.publish_driver(state, launch.DRIVER_SRC, a.dry_run)
    spec_path = launch.write_spec(state, batch.batch, p, driver, a.dry_run)
    c.emit(f"{p.run_id}: {a.stage} on {p.host} {p.root}" + (" (dry)" if a.dry_run else ""),
           {"run_id": p.run_id, "batch": batch.batch, "host": p.host, "root": p.root, "spec": p.spec})
    if a.dry_run:
        return 0
    now = int(time.time())
    c.ledger.upsert_run({"run_id": p.run_id, "batch": batch.batch, "label": job.label, "config": job.config,
                         "build_tag": p.build_tag, "src": p.src, "dirty": int("-dirty" in p.src), "host": p.host,
                         "root": p.root, "created": now, "phase": "setup", "state": "running", "started": now,
                         "tree_id": p.values.get("tree_id") or p.run_id})
    c.ledger.add_event("user", p.run_id, "run", f"{a.stage} on {run_id}" + (f" from {a.from_}" if a.from_ else ""))
    launch.start_driver(c.ssh, str(p.host), driver, spec_path, spec_path.with_name(f"{p.run_id}.driver.log"),
                        project.site.env)
    return 0


def cmd_keep(c: Ctx, a: argparse.Namespace) -> int:
    """Write the keep file of a run."""
    row = c.resolve(a.handle)
    if not board.is_live(row):
        c.emit(f"{row['run_id']}: already {row['phase']}")
        return 2
    hours = a.hours if a.hours is not None or a.ack else 12
    c.emit(_keep(c, row, hours, a.ack or None, "user") + (" (dry)" if a.dry_run else ""))
    return 0


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
               src=a.src, dirty=0, host=a.host or "", root=root or None, created=now, phase=a.phase, state="imported",
               stage="", step=-1, exit=0 if a.phase == "done" else None, started=now, updated=now,
               counts=json.dumps({}), tree_id=a.run_id)
    where = f"{a.host}:{root}" if root else f"results {results}"
    text = f"{where} as {a.label}@{a.batch}" + (f": {a.why}" if a.why else "")
    if not a.dry_run:
        c.ledger.upsert_batch(dict(batch=a.batch, project=c.project.project, source=a.src, created=now))
        c.ledger.upsert_run(row)
        c.ledger.set_params(a.run_id, {k: row[k] for k in ("config", "build_tag", "src") if row[k]}, "import")
        if results:
            text += f", {_import_results(c, row, results, tasks)} metrics"
        c.ledger.add_event("user", a.run_id, "import", text)
    c.emit(f"imported {text}" + (" (dry)" if a.dry_run else ""), row)
    return 0


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
    return sum(c.ledger.add_metric(r) for r in rows if r["value"] is not None)


def cmd_export(c: Ctx, a: argparse.Namespace) -> int:
    """Write a frozen snapshot of one design."""
    labels = a.labels.split(",") if a.labels else None
    manifest = export.export(c.project, c.ledger, a.design, Path(a.out), labels, a.dry_run, a.with_logs)
    if not a.dry_run:
        c.ledger.add_event("user", "", "export", f"{a.design} -> {a.out}")
    c.emit(f"{a.out}: {len(manifest['runs'])} runs, {len(manifest['files'])} files" + (" (dry)" if a.dry_run else ""),
           manifest)
    return 0


def cmd_stop(c: Ctx, a: argparse.Namespace) -> int:
    """Stop one run through the driver, or the stop file with --after-task."""
    row = c.resolve(a.handle)
    hb = c.heartbeat(row)
    if row.get("state") == "queued":
        # The watcher launches every row in state queued; a stopped row never starts.
        c.emit(f"{row['run_id']}: queued, marked stopped" + (" (dry)" if a.dry_run else ""),
               {"run_id": row["run_id"], "stopped": True})
        if not a.dry_run:
            c.ledger.upsert_run({"run_id": row["run_id"], "state": "stopped"})
            c.ledger.add_event("user", row["run_id"], "stop", f"queued: {a.why}")
        return 0
    if not board.is_live(hb or row):
        c.emit(f"{row['run_id']}: already {(hb or row).get('phase')}")
        return 2
    # The driver's own handler ends the run in seconds; limits.grace_s is the watcher's delay.
    ok = launch.stop(c.ssh, c.ledger, row, hb, after_task=a.after_task, now=a.now, grace_s=30 if a.now else 60,
                     dry_run=a.dry_run, why=a.why, state=c.project.state)
    c.data = {"run_id": row["run_id"], "stopped": ok}
    if not ok and not a.now:
        print(f"{row['run_id']}: still alive; use --now")
    return 0 if ok else 3


def cmd_retire(c: Ctx, a: argparse.Namespace) -> int:
    """Remove the run tree, or the prune targets, after the guard; --batch marks RETIRED."""
    if not a.handle and not a.batch:
        raise Refuse("retire needs a handle or --batch")
    project, dry = c.project, " (dry)" if a.dry_run else ""
    rows = c.ledger.runs(batch=a.batch) if a.batch else [c.resolve(a.handle)]
    if a.batch and not rows and not (project.state / a.batch).is_dir():
        c.emit(f"no runs in batch {a.batch}")
        return 2
    checked, retiring = [], {r["run_id"] for r in rows}
    for row in rows:
        run_id, hb = row["run_id"], c.heartbeat(row)
        host, root = row.get("host") or hb.get("host"), row.get("root") or hb.get("root")
        targets = _retire_targets(c, row, hb, str(root), a) if root else []
        pid = hb.get("driver_pid")
        if root and board.is_live(hb) and pid and c.ssh.pid_alive(str(host), int(pid)):
            raise Refuse(f"{run_id}: driver {pid} is alive on {host}; stop it first")
        # A driver writes its first heartbeat within seconds; none after dead_s means it never came up.
        if root and not hb and board.is_live(row) and time.time() - (row.get("started") or 0) < project.limits.dead_s:
            raise Refuse(f"{run_id}: no heartbeat yet; wait for the driver, then stop it first")
        if root:
            _refuse_shared_root(c, row, str(host), str(root), retiring, bool(a.prune))
        checked.append((row, hb, host, root, targets))
    worktree = _worktree_target(c, a.batch) if a.batch and not a.prune else None
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
                config.save_json(project.state / str(row["batch"]) / f"{run_id}.json", hb)
            c.ledger.upsert_run({"run_id": run_id, "phase": phase, "exit": 1, "state": "retired"})
        c.ledger.add_event("user", run_id, "prune" if a.prune else "retire", f"{a.why}: " + (" ".join(targets) or "no tree"))
        done.append(run_id)
    if a.batch and not a.prune and not a.dry_run:
        (project.state / a.batch).mkdir(parents=True, exist_ok=True)
        (project.state / a.batch / "RETIRED").touch()
        c.ledger.mark_batch_retired(a.batch)
    if worktree is not None:
        real = (worktree / ".git").exists()
        print(f"{a.batch}: {'git worktree remove --force' if real else 'rm -rf'} {worktree}{dry}")
        if not a.dry_run:
            try:
                if real:
                    runid.git("worktree", "remove", "--force", str(worktree), cwd=project.source.repo)
                else:
                    shutil.rmtree(worktree)
                c.ledger.add_event("user", "", "retire", f"{a.why}: worktree {worktree}")
            except (runid.GitError, OSError) as e:
                failed += 1
                print(f"{a.batch}: worktree not removed: {e}", file=sys.stderr)
    c.data = {"retired": done, "failed": failed}
    return 3 if failed else 0


def _worktree_target(c: Ctx, batch: str) -> Path | None:
    """The staged tree of the batch's source, when no other batch that is not retired has it; guarded."""
    rows = {b["batch"]: b for b in c.ledger.batches()}
    src = str((rows.get(batch) or {}).get("source") or "")
    if not src or any(b["batch"] != batch and not b.get("retired") and b.get("source") == src for b in rows.values()):
        return None
    path = c.project.source.worktrees / src
    if not path.is_dir():
        return None
    if path.resolve() == c.project.source.repo.resolve():
        raise Refuse(f"{path} is the source repository")
    return assert_safe_target(path, c.project.safety.marker, c.project.safety.min_depth)


def _refuse_shared_root(c: Ctx, row: Row, host: str, root: str, retiring: set[str], prune: bool) -> None:
    """A tree that another run still uses is never a delete target of this call.

    Every power run on a reused tree has that tree as its root. Removing it for one
    run would take the netlist of the others, and of a live driver among them.
    """
    others = [r for r in c.ledger.runs() if r["run_id"] not in retiring and r.get("root") == root
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
        watch.cycle(project, c.ssh, c.ledger, [], dry_run=True)
        return 0
    notifiers = _notifiers(c)
    if a.check:
        return watch.check(project, notifiers)
    return watch.run_forever(project, c.ssh, c.ledger, notifiers, once=a.once)


def _notifiers(c: Ctx) -> list:
    project = c.project
    bot = Ctx(c.a)
    bot._project = project
    # The bot polls in its own thread.
    bot._ledger = Ledger(project.data / "edr.db", threads=True)
    return make_notifiers(project.site, project, bot.ledger, Actions(bot))


# --- parser and main

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # Bad input exits 1 like a refused guard, not the 2 of argparse.
        self.print_usage(sys.stderr)
        print(f"edr: {message}", file=sys.stderr)
        raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="edr", description="Run flows on hosts, keep a ledger, watch, export.")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--version", action="version", version=f"edr {__version__}")
    p.set_defaults(dry_run=False)
    sub = p.add_subparsers(dest="verb", metavar="verb", required=True)

    def verb(name: str, help_: str, write: bool = False, why: bool = False) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=globals()["cmd_" + name])
        if write:
            s.add_argument("--dry-run", action="store_true", help="print what would happen and write nothing")
        if why:
            s.add_argument("--why", required=True, help="the reason; it goes into the events table")
        return s

    s = verb("status", "the board, or one run")
    s.add_argument("handle", nargs="?", help="label@batch, a run id prefix, or #n from the last board")
    s.add_argument("--batch", metavar="B", help="one batch; default EDR_BATCH, else every batch")
    s.add_argument("--narrow", action="store_true", help="48 columns, two lines per live run")
    s.add_argument("--watch", action="store_true", help="redraw every heartbeat_s")
    s.add_argument("--live", action="store_true", help="ask each host whether the driver exists")
    s.add_argument("--triage", action="store_true", help="every run not running, with a proposed command")
    s = verb("events", "the last events")
    s.add_argument("--since", metavar="T", help="30m, 2h, 1d or seconds")
    s.add_argument("--run", metavar="HANDLE", help="the events of one run")
    s.add_argument("-n", type=int, default=50, help="the last N events; default 50")
    verb("hosts", "probe every host").add_argument("--narrow", action="store_true", help="host, free cores, RAM and space only")
    verb("lic", "probe every licence")
    s = verb("metrics", "the metrics of one design")
    s.add_argument("--design", required=True, metavar="SRC", help="the exact source tag of the runs, as in the run id")
    s.add_argument("--stage", metavar="S", help="the metrics of one stage")
    s.add_argument("--step", type=int, metavar="N", help="the metrics of one step number")
    s.add_argument("--csv", action="store_true", help="CSV on stdout")
    verb("init", "write edr.toml and the watch unit here", write=True).add_argument("--site", required=True, metavar="DIR", help="the site directory, or a site.toml path")
    verb("check", "load everything, probe the hosts, check the hooks")
    s = verb("stage", "stage a ref as a worktree, or a dirty tree as a snapshot", write=True)
    s.add_argument("ref", nargs="?", help="default: source.ref")
    s.add_argument("--dirty", metavar="DIR", help="snapshot this working tree instead of a ref")
    verb("plan", "render the run specs of a batch; writes nothing", write=True).add_argument(
        "batch", nargs="?", help="the batch name; default EDR_BATCH, else the newest")
    s = verb("launch", "start one driver per job of a batch", write=True)
    s.add_argument("batch", nargs="?", help="the batch name; default EDR_BATCH, else the newest")
    s.add_argument("--only", metavar="L", help="labels, comma separated")
    s.add_argument("--allow-dirty", action="store_true", help="launch a dirty snapshot source")
    s = verb("run", "more work on the tree of an existing run", write=True)
    s.add_argument("handle", help="label@batch, a run id prefix, or #n from the last board")
    s.add_argument("--stage", metavar="S", help="the stage to run on the tree")
    s.add_argument("--tasks", nargs="+", metavar="ID", help="the tasks of a task group; default the job's")
    s.add_argument("--from", dest="from_", metavar="CHECKPOINT", help="resume the stage from this checkpoint")
    s.add_argument("--on", metavar="HOST", help="the host; default auto")
    s.add_argument("--parallel", type=int, metavar="N", help="tasks at once; default the stage's parallel")
    s.add_argument("--collect", metavar="NAME", help="fetch a collect_on_request list instead")
    s = verb("keep", "add hours to the running stage or task; --ack cancels a pending kill", write=True)
    s.add_argument("handle", help="label@batch, a run id prefix, or #n from the last board")
    s.add_argument("--hours", type=int, metavar="N", help="default 12")
    s.add_argument("--ack", action="store_true", help="cancel the pending kill of the run")
    s = verb("import", "record a run tree that edr did not make, or its collected results", write=True)
    s.add_argument("--run-id", required=True, dest="run_id", help="the run id; it must start with YYYYMMDD_HHMM_")
    s.add_argument("--label", required=True, help="the label of the run")
    s.add_argument("--config", required=True, help="the configuration name of the run")
    s.add_argument("--src", required=True, metavar="SRC", help="the source tag of the tree")
    s.add_argument("--host", help="the host of the tree")
    s.add_argument("--root", metavar="PATH", help="the tree on the host")
    s.add_argument("--results", metavar="DIR", help="collected files in the run layout; linked as data/results/<run id>")
    s.add_argument("--tasks", nargs="+", metavar="ID", help="the tasks whose files the results hold")
    s.add_argument("--batch", default="imported", help="the batch to record it in; default imported")
    s.add_argument("--phase", default="done", help="the terminal phase; default done")
    s.add_argument("--build-tag", dest="build_tag", metavar="TAG", help="the build tag of the run")
    s.add_argument("--why", default="", help="the reason; it goes into the events table")
    s = verb("export", "a frozen snapshot of one design", write=True)
    s.add_argument("--design", required=True, metavar="SRC", help="the exact source tag of the runs, as in the run id")
    s.add_argument("--out", required=True, metavar="DIR", help="the directory to write; it must be absent or empty")
    s.add_argument("--labels", metavar="a,b", help="these labels only, comma separated")
    s.add_argument("--with-logs", dest="with_logs", action="store_true", help="also copy log/ directories and *.log files")
    s = verb("stop", "stop one run", write=True, why=True)
    s.add_argument("handle", help="label@batch, a run id prefix, or #n from the last board")
    s.add_argument("--after-task", action="store_true", help="write the stop file; the running task ends first")
    s.add_argument("--now", action="store_true", help="SIGKILL after 30 s")
    s = verb("retire", "remove the run tree, or its prune targets", write=True, why=True)
    s.add_argument("handle", nargs="?", help="label@batch, a run id prefix, or #n from the last board")
    s.add_argument("--batch", metavar="B", help="every run of the batch, then mark it RETIRED")
    s.add_argument("--prune", metavar="T", help="remove the prune targets named T instead of the tree")
    s.add_argument("--uncollected", action="store_true", help="remove a tree whose results were never collected")
    s = verb("watch", "the watcher", write=True)
    s.add_argument("--once", action="store_true", help="one cycle; exit 1 when it failed")
    s.add_argument("--check", action="store_true", help="exit 1 when watch.json is older than three cycles")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run one verb and return its exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    c, buf = Ctx(a), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf) if a.json else contextlib.nullcontext():
            code = a.fn(c, a)
    except (Refuse, ConfigError, stagectl.StageError, runid.GitError) as e:
        print(f"edr: {e}", file=sys.stderr)
        code = 1
    except HostError as e:
        print(f"edr: {e}", file=sys.stderr)
        code = 3
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # the exit code and the --json envelope must survive any fault
        logging.getLogger("edr").exception("unhandled")
        print(f"edr: {e}", file=sys.stderr)
        code = 1
    finally:
        c.close()
    if a.json:
        print(json.dumps({"code": code, "data": c.data, "output": buf.getvalue()}, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())

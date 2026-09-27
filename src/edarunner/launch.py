"""Plan, launch and stop runs. See docs/design.md sections 4 and 6."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config, hosts, sync
from .config import ConfigError
from .guards import Refuse, assert_safe_target
from .ledger import Ledger
from .model import Batch, Budget, Job, Needs, Project, Stage, Task

DRIVER_SRC = Path(__file__).resolve().parent / "driver" / "edr_driver.py"
DATE_FMT = "%Y%m%d_%H%M"
_ID_RE = re.compile(r"^[A-Za-z_]\w*$")
_SPEC_LIMITS = ("host_free_min_gb", "streak", "heartbeat_s", "gate_max_s")


@dataclass
class RunPlan:
    run_id: str
    label: str
    host: str | None
    root: str
    spec: dict[str, Any]
    queued: bool
    problems: list[str] = field(default_factory=list)
    src: str = ""
    build_tag: str = ""
    reuse: str = ""  # the run id whose tree this run continues
    values: dict[str, object] = field(default_factory=dict)


# --- date pin and build tag

def pin_date(state: Path, batch: str, dry_run: bool = False) -> str:
    """Read the date pin of a batch, or write one now; a dry run writes nothing."""
    pin = Path(state) / batch / "RUN_DATE"
    try:
        date = pin.read_text().strip()
    except OSError:
        date = ""
    if date:
        return date
    date = time.strftime(DATE_FMT)
    if not dry_run:
        pin.parent.mkdir(parents=True, exist_ok=True)
        tmp = pin.with_name(pin.name + ".tmp")
        tmp.write_text(date + "\n")
        os.replace(tmp, pin)
    return date


def build_tag(project: Project, job: Job, src: str = "") -> str:
    """The build tag of a job: the source hook, else config plus one token per override.

    The hook gets the worktree of `src` as a third argument when one exists, so it
    can read the configuration table and the interpreter of that source.
    """
    if project.source.build_tag:
        fn, name = config.load_hook(project.root, project.source.build_tag)
        worktree = None
        if src:
            candidate = project.source.worktrees / src
            worktree = str(candidate) if candidate.is_dir() else None
        try:
            try:
                return str(fn(job.config, job.overrides, worktree))
            except TypeError:
                return str(fn(job.config, job.overrides))
        except Exception as e:  # a hook fault is a plan problem, never a traceback
            raise ConfigError(f"build_tag hook {name}: {e}") from e
    return job.config + "".join(f"_{k}{v}" for k, v in job.overrides.items())


# --- spec rendering

def _merge(base: Any, over: Any) -> dict[str, Any]:
    """The fields of `base`, with every field of `over` that differs from its default."""
    out = asdict(base)
    if over is not None:
        default = asdict(type(base)())
        out.update({k: v for k, v in asdict(over).items() if v != default[k]})
    return out


def _budget(base: Budget, over: Budget | None) -> dict[str, Any]:
    merged = _merge(base, over)
    return {k: v for k, v in merged.items() if v not in (None, False) and not (k == "per" and v == "stage")}


def _licence(project: Project, ref: str | dict[str, int] | None, values: dict[str, object]) -> dict[str, Any] | None:
    if not ref:
        return None
    name, seats = (ref, None) if isinstance(ref, str) else next(iter(ref.items()))
    lic = project.site.licences[name]
    return {"name": name, "feature": lic.feature, "floor": lic.floor,
            "seats_per_task": lic.seats_per_task if seats is None else seats,
            "probe": config.render(lic.probe, values)}


def _task_spec(stage: Stage, task: Task, values: dict[str, object]) -> dict[str, Any]:
    needs = _merge(stage.needs, task.needs)
    needs.pop("licence")
    v = {**values, **{f"task.{k}": x for k, x in task.fields.items()}, "cores": needs["cores"]}
    v["task_dir"] = os.path.normpath(os.path.join(str(values["root"]), config.render(stage.task_dir, v)))
    return {"id": task.id, "cmd": config.render(stage.cmd, v), "dir": v["task_dir"],
            "needs": needs, "budget": _budget(stage.budget, task.budget)}


def _stage_spec(project: Project, stage: Stage, tasks: list[Task], values: dict[str, object]) -> dict[str, Any]:
    needs = _merge(stage.needs, None)
    ref = needs.pop("licence")
    v = {**values, "cores": needs["cores"]}
    out: dict[str, Any] = {"name": stage.name, "cwd": os.path.normpath(os.path.join(str(v["root"]), stage.cwd)),
                           "needs": needs}
    lic = _licence(project, ref, v)
    if lic:
        out["licence"] = lic
    if budget := _budget(stage.budget, None):
        out["budget"] = budget
    if stage.retry:
        out["retry"] = asdict(stage.retry)
    if not stage.is_group:
        out["cmd"] = config.render(stage.cmd, v)
        if stage.resume:
            # The driver fills {checkpoint} at a resume.
            out["resume"] = config.render(stage.resume, {**v, "checkpoint": "{checkpoint}"})
        if stage.progress:
            out["progress"] = config.render(stage.progress, v)
        if stage.steps:
            out["steps"] = list(stage.steps)
        return out
    out["parallel"] = stage.parallel
    if stage.prepare:
        out["prepare"] = config.render(stage.prepare, v)
    if stage.after_each:
        # The driver fills {task_dir} per task.
        out["after_each"] = config.render(stage.after_each, {**v, "task_dir": "{task_dir}"})
    out["tasks"] = [_task_spec(stage, t, v) for t in tasks]
    return out


def _env(project: Project, v: dict[str, object]) -> dict[str, str]:
    """The site env, then the project env rendered per run; `$VAR` expands on the host."""
    env = dict(project.site.env)
    for k, val in project.env.items():
        env[k] = config.render(val, v)
    return env


def _spec(project: Project, batch: Batch, job: Job, names: list[str], tasks: list[Task],
          v: dict[str, object]) -> dict[str, Any]:
    state_dir = project.state / batch.batch
    run_id = str(v["run_id"])
    return {
        "schema": 1, "run_id": run_id, "batch": batch.batch, "project": project.project,
        "label": job.label, "config": job.config, "host": v["host"], "root": v["root"],
        "state_file": str(state_dir / f"{run_id}.json"), "queue_dir": str(state_dir / f"{run_id}.queue"),
        "shell": "/bin/bash", "env": _env(project, v),
        "limits": {k: getattr(project.limits, k) for k in _SPEC_LIMITS},
        "netlist_stage": v["netlist_stage"],
        "start_at": {"stage": names[0], "checkpoint": None},
        "stages": [_stage_spec(project, project.stages[n], tasks, v) for n in names],
    }


# --- plan

def _reuse_row(ledger: Ledger, reuse: dict[str, object]) -> dict[str, Any]:
    if "run_id" in reuse:
        row = ledger.run(ledger.resolve(str(reuse["run_id"])))
    else:
        rows = [r for r in ledger.runs() if r["label"] == reuse["label"]]
        row = rows[-1] if rows else None
    if row is None or not row.get("root") or not row.get("host"):
        raise KeyError(f"reuse {reuse}: no run with a host and a root in the ledger")
    return row


def _check_overrides(project: Project, job: Job, names: list[str]) -> list[str]:
    bad = [k for k in job.overrides if not _ID_RE.match(k)]
    problems = [f"override key {k!r} is not a KEY=VALUE name" for k in bad]
    texts = [getattr(project.stages[n], key) for n in names for key in ("cmd", "resume", "prepare")]
    if job.overrides and not any("{overrides}" in t for t in texts):
        problems.append("overrides given, but no stage of the job uses {overrides}")
    return problems


def _plan_job(project: Project, batch: Batch, job: Job, ledger: Ledger, date: str,
              probes: dict[str, hosts.HostProbe], errors: dict[str, str], placed: dict[str, str | None]) -> RunPlan:
    names = job.stages or list(project.stages)
    problems = _check_overrides(project, job, names)
    src, host, mount, root, reused, tag, tree = batch.source, None, "", "", "", "", ""
    if job.reuse:
        try:
            row = _reuse_row(ledger, job.reuse)
            host, root, src, reused = row["host"], row["root"], row["src"], row["run_id"]
            # The tag and the tree id belong to the tree, through any chain of reuse.
            tag = row.get("build_tag") or ""
            tree = row.get("tree_id") or reused
            if job.host not in ("auto", host):
                problems.append(f"reuse of {reused} needs host {host}, the job says {job.host}")
        except KeyError as e:
            problems.append(str(e))
    if not tag:
        try:
            tag = build_tag(project, job, src)
        except ConfigError as e:
            problems.append(str(e))
            tag = job.config
    v = config.placeholders(project, date=date, batch=batch.batch, label=job.label, config=job.config,
                            build_tag=tag, src=src, overrides=job.overrides,
                            netlist_stage=11 if job.netlist_stage is None else job.netlist_stage)
    run_id = config.render(project.source.run_id, v)
    if not job.reuse:
        host = job.host if job.host != "auto" else placed.get(job.label)
        if host in errors:
            problems.append(f"{host}: {errors[host]}")
            host = None
        if host:
            mount = probes[host].mount
            # An empty mount once gave a root of /<user>/... and a delete target outside every tree.
            if not mount:
                problems.append(f"{host}: no writable scratch found")
            root = f"{mount}/{config.render(project.run_prefix, v)}/{run_id}"
    # {tree_id} names the tree the flow writes in: the reused run's id, else this run's.
    v.update(run_id=run_id, tree_id=tree or run_id, host=host, mount=mount, root=root)
    spec: dict[str, Any] = {}
    if host and not problems:
        try:
            assert_safe_target(root, project.safety.marker, project.safety.min_depth)
            tasks = [config.resolve_task(project, t) for t in job.tasks]
            for n in names:
                if project.stages[n].is_group and not tasks:
                    problems.append(f"stage {n} is a task group and the job lists no tasks")
            spec = _spec(project, batch, job, names, tasks, v)
        except (ConfigError, Refuse, KeyError) as e:
            problems.append(str(e))
    return RunPlan(run_id=run_id, label=job.label, host=host, root=root, spec=spec,
                   queued=host is None and not problems, problems=problems, src=src, build_tag=tag,
                   reuse=reused, values=v)


def plan(project: Project, batch: Batch, ssh: hosts.Ssh, ledger: Ledger, date: str | None = None,
         only: list[str] | None = None, probes: dict[str, hosts.HostProbe] | None = None) -> list[RunPlan]:
    """Render every job of a batch into a RunPlan; a problem is reported in the plan, not raised.

    `probes` are the results of a caller who probed already; the hosts are then not probed again.
    """
    date = date or pin_date(project.state, batch.batch, dry_run=True)
    jobs = [j for j in batch.jobs if not only or j.label in only]
    wanted = {j.host for j in jobs if not j.reuse}
    names = (set(project.site.hosts) if "auto" in wanted else set()) | (wanted - {"auto"})
    errors: dict[str, str] = {}
    if probes is None:
        probes = {}
        for h in sorted(names):
            try:
                probes[h] = ssh.probe(h)
            except hosts.HostError as e:
                errors[h] = str(e)
    else:
        errors = {h: "no probe of the host" for h in names if h not in probes}
    auto = [j for j in jobs if j.host == "auto" and not j.reuse]
    placed = hosts.place(project, auto, probes, {h: p.our_runs for h, p in probes.items()}) if auto else {}
    return [_plan_job(project, batch, j, ledger, date, probes, errors, placed) for j in jobs]


# --- launch

def write_spec(state: Path, batch: str, plan_: RunPlan, dry_run: bool = False) -> Path:
    """Write <state>/<batch>/<run_id>.spec.json by a temporary file and rename."""
    path = Path(state) / batch / f"{plan_.run_id}.spec.json"
    if dry_run:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan_.spec, indent=1) + "\n")
    os.replace(tmp, path)
    return path


def _dq(text: str) -> str:
    # Double quotes keep $VAR for the host's shell; single quotes would not.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'


def start_driver(ssh: hosts.Ssh, host: str, driver_path: Path, spec_path: Path, log_path: Path,
                 env: dict[str, str]) -> int | None:
    """Start the driver on `host` with the host's python3; return its pid when known."""
    if host == "local":
        full = dict(os.environ)
        full.update({k: os.path.expandvars(v) for k, v in env.items()})
        with open(log_path, "ab") as log:
            p = subprocess.Popen(["python3", str(driver_path), str(spec_path)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, env=full, start_new_session=True)
        return p.pid
    exports = "".join(f"export {k}={_dq(v)}; " for k, v in env.items())
    # The interpreter is the host's own python3, resolved before the site env: a tool PATH
    # once put a Python 3.4 first, and the driver died at its first subprocess.run.
    cmd = (f"py=$(command -v python3); {exports}setsid nohup \"$py\" {shlex.quote(str(driver_path))} "
           f"{shlex.quote(str(spec_path))} > {shlex.quote(str(log_path))} 2>&1 < /dev/null & echo $!")
    rc, out, err = ssh.run(host, cmd)
    if rc != 0:
        raise hosts.HostError(f"{host}: rc {rc}: {err.strip()}")
    last = out.split()[-1] if out.split() else ""
    return int(last) if last.isdigit() else None


def _src_dir(project: Project, batch: Batch, src_dir: Path | None) -> Path:
    if src_dir is not None:
        return Path(src_dir)
    try:
        from . import stagectl
    except ImportError:
        raise Refuse("no src_dir given and stagectl is missing") from None
    return Path(stagectl.find(project, batch.source))


def launch(project: Project, batch: Batch, ssh: hosts.Ssh, ledger: Ledger, dry_run: bool = False,
           only: list[str] | None = None, allow_dirty: bool = False, stagger_s: int | None = None,
           src_dir: Path | None = None) -> list[dict[str, Any]]:
    """Pin the date, publish the driver, sync, write the specs and start one driver per run."""
    if "-dirty" in batch.source and not allow_dirty:
        raise Refuse(f"source {batch.source} is dirty; pass --allow-dirty")
    state = project.state
    date = pin_date(state, batch.batch, dry_run)
    plans = plan(project, batch, ssh, ledger, date=date, only=only)
    driver = sync.publish_driver(state, batch.batch, DRIVER_SRC, dry_run)
    if not dry_run:
        ledger.upsert_batch({"batch": batch.batch, "project": project.project, "source": batch.source, "run_date": date})
    stagger = project.limits.stagger_s if stagger_s is None else stagger_s
    out: list[dict[str, Any]] = []
    started = 0
    for p in plans:
        row = {"run_id": p.run_id, "label": p.label, "host": p.host, "root": p.root, "queued": p.queued,
               "problems": p.problems, "started": False, "pid": None}
        out.append(row)
        spec_path = state / batch.batch / f"{p.run_id}.spec.json"
        if p.host and not p.problems and spec_path.exists():
            p.problems.append(f"already launched: {spec_path} exists")
        if p.queued and not dry_run:
            ledger.upsert_run({**_run_row(p, batch), "state": "queued"})
        if p.queued or p.problems:
            print(f"{p.run_id}: " + ("queued" if p.queued else "; ".join(p.problems)))
            continue
        if started and stagger > 0:
            time.sleep(stagger)
        if not p.reuse:
            ok = sync.sync_tree(ssh, p.host, _src_dir(project, batch, src_dir), p.root, project.sync.exclude,
                                project.safety.marker, project.safety.min_depth, dry_run)
            if ok and project.sync.after and not dry_run:
                ok = sync.run_after_hook(project.site, project, project.sync.after, p.values)
            if not ok:
                p.problems.append("sync failed")
                continue
        path = write_spec(state, batch.batch, p, dry_run)
        log = path.with_name(f"{p.run_id}.driver.log")
        print(f"{p.run_id}: {p.host} {p.root}" + (" (dry)" if dry_run else ""))
        if dry_run:
            continue
        ledger.upsert_run({**_run_row(p, batch), "phase": "setup", "state": "running", "started": int(time.time())})
        ledger.add_event("user", p.run_id, "launch", f"{p.host} {p.root}")
        row["pid"] = start_driver(ssh, p.host, driver, path, log, project.site.env)
        row["started"] = True
        started += 1
    return out


def _run_row(p: RunPlan, batch: Batch) -> dict[str, Any]:
    return {"run_id": p.run_id, "batch": batch.batch, "label": p.label, "config": p.spec.get("config") or p.values.get("config"),
            "build_tag": p.build_tag, "src": p.src, "dirty": int("-dirty" in p.src), "host": p.host, "root": p.root or None,
            "tree_id": p.values.get("tree_id") or p.run_id, "created": int(time.time())}


# --- stop

def stop(ssh: hosts.Ssh, ledger: Ledger, run_row: dict[str, Any], heartbeat: dict[str, Any], after_task: bool = False,
         now: bool = False, grace_s: int = 30, dry_run: bool = False, why: str = "", state: Path | None = None,
         actor: str = "user") -> bool:
    """Stop one run: the stop file with `after_task`, else TERM to the driver and every pgid, KILL with `now`."""
    run_id, host = run_row["run_id"], run_row["host"]
    if after_task:
        if state is None:
            raise ValueError("after_task needs the state directory")
        path = Path(state) / run_row["batch"] / f"{run_id}.stop"
        print(f"{run_id}: write {path}" + (" (dry)" if dry_run else ""))
        if not dry_run:
            path.write_text("after-task\n")
            ledger.add_event(actor, run_id, "stop", f"after-task: {why}")
        return True
    pid, pgids = heartbeat.get("driver_pid"), [g for g in heartbeat.get("pgids") or [] if g]
    print(f"{run_id}: TERM driver {pid} and pgids {pgids} on {host}" + (" (dry)" if dry_run else ""))
    if dry_run:
        return True
    # The driver's own handler kills its groups and writes KILLED; the pgids follow for a dead driver.
    _signal(ssh, host, pid, pgids, "TERM")
    end = time.time() + grace_s
    alive = pid is not None
    while alive and time.time() < end:
        time.sleep(0.5)
        alive = ssh.pid_alive(host, pid)
    if alive and now:
        _signal(ssh, host, pid, pgids, "KILL")
        alive = ssh.pid_alive(host, pid)
    ledger.add_event(actor, run_id, "stop", f"{why} [driver {pid}, pgids {pgids}, {'killed' if now else 'term'}, "
                     f"{'alive' if alive else 'ended'}]")
    return not alive


def _signal(ssh: hosts.Ssh, host: str, pid: int | None, pgids: list[int], sig: str) -> None:
    # pid 0 or a negative number would signal the shell's group or every process.
    if pid is not None and (not isinstance(pid, int) or pid <= 1):
        raise Refuse(f"'{pid}' is not a pid")
    if pid:
        ssh.run(host, f"kill -{sig} {pid}")
    for pgid in pgids:
        ssh.kill_pgid(host, pgid, sig)

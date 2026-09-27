"""Plan, launch and stop runs: the run spec, and the driver start and stop through the backend."""

from __future__ import annotations

import hashlib
import os
import re
import string
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, board, collect, config, hosts, sync
from .backend import Backend, Handle, Request, check_pid, gone, make_backend, run_handle
from .config import ConfigError
from .guards import Refuse, assert_safe_target
from .db import Database
from .model import SCHEDULERS, Batch, Budget, Job, Project, Stage, Task

DRIVER_SRC = Path(__file__).resolve().parent / "driver" / "edr_driver.py"
DATE_FMT = "%Y%m%d_%H%M"
_ID_RE = re.compile(r"^[A-Za-z_]\w*$")
_SPEC_LIMITS = ("host_free_min_gb", "streak", "heartbeat_s", "gate_max_s", "lease_s")


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
    tree_host: str = ""  # where the tree is synced: the host, or `local` for a tree under a scheduler's tree_root
    reuse: str = ""  # the run id whose tree this run continues, or whose archive it restores
    restore: str = ""  # the collect_on_request list copied back from data/results of `reuse`
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
        config.save_text(pin, date + "\n")
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
    return "_".join([job.config] * bool(job.config) + [f"{k}{v}" for k, v in job.overrides.items()])


def render_run_id(template: str, values: dict[str, object]) -> str:
    """Render the run id template; an empty placeholder takes one `_` or `-` next to it along."""
    for key, value in values.items():
        if value == "":
            template = re.sub(rf"[_-]\{{{re.escape(key)}\}}|\{{{re.escape(key)}\}}[_-]?", "", template)
    return config.render(template, values)


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


def _tools(project: Project, needs: dict[str, int], values: dict[str, object]) -> list[dict[str, Any]]:
    """The gate of a stage or task: name, seats, the rendered probe argv and the lease directory of each needed tool with a probe."""
    return [{"name": n, "seats": seats, "probe": [config.render(a, values) for a in project.site.tools[n].probe],
             "leases": str(lease_dir(project.state_dir, n))}
            for n, seats in needs.items() if project.site.tools[n].probe]


def lease_dir(state: Path, tool: str) -> Path:
    """The directory of the seat leases of one tool, shared by every run of the project."""
    return Path(state) / "leases" / tool


def _task_spec(project: Project, stage: Stage, task: Task, values: dict[str, object]) -> dict[str, Any]:
    needs = _merge(stage.needs, task.needs)
    tools = needs.pop("tools")
    needs.pop("ram_gb")  # the scheduler's, not the driver's
    v = {**values, **{f"task.{k}": x for k, x in task.fields.items()}, "cores": needs["cores"]}
    v["task_dir"] = os.path.normpath(os.path.join(str(values["root"]), config.render(stage.task_dir, v)))
    out = {"id": task.id, "cmd": config.render(stage.cmd, v), "dir": v["task_dir"],
           "needs": needs, "budget": _budget(stage.budget, task.budget)}
    if task.needs and task.needs.tools:
        out["tools"] = _tools(project, tools, v)
    return out


def _stage_spec(project: Project, stage: Stage, tasks: list[Task], values: dict[str, object]) -> dict[str, Any]:
    needs = _merge(stage.needs, None)
    needs.pop("ram_gb")
    v = {**values, "cores": needs["cores"]}
    out: dict[str, Any] = {"name": stage.name, "cwd": os.path.normpath(os.path.join(str(v["root"]), stage.cwd)),
                           "needs": needs}
    if tools := _tools(project, needs.pop("tools"), v):
        out["tools"] = tools
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
    out["tasks"] = [_task_spec(project, stage, t, v) for t in tasks]
    return out


def _env(project: Project, v: dict[str, object]) -> dict[str, str]:
    """The site env, then the project env rendered per run, then `EDR_SRC`, `EDR_RUN_ID` and `EDR_TREE_ID`.

    A project value builds on the site: its `$NAME` or `${NAME}` takes the site value of
    NAME when the project does not set NAME under another key. The host expands the rest.
    """
    env = dict(project.site.env)
    for k, val in project.env.items():
        site = {n: s for n, s in project.site.env.items() if n == k or n not in project.env}
        env[k] = string.Template.pattern.sub(
            lambda m, site=site: site.get(m.group("named") or m.group("braced") or "", m.group(0)),
            config.render(val, v))
    env.update(EDR_SRC=str(v.get("src") or ""), EDR_RUN_ID=str(v["run_id"]), EDR_TREE_ID=str(v.get("tree_id") or v["run_id"]))
    return env


def _spec(project: Project, batch: Batch, job: Job, names: list[str], tasks: list[Task],
          v: dict[str, object], stages: list[Stage] | None = None) -> dict[str, Any]:
    """The run spec; `stages` replaces the stages that `names` picks from the project."""
    stages = stages or [project.stages[n] for n in names]
    state_dir = project.state_dir / batch.batch
    run_id = str(v["run_id"])
    spec: dict[str, Any] = {
        "schema": 1, "run_id": run_id, "batch": batch.batch, "project": project.project,
        "label": job.label, "config": job.config, "vars": job.vars, "host": v["host"], "root": v["root"],
        "state_file": str(state_dir / f"{run_id}.json"), "queue_dir": str(state_dir / f"{run_id}.queue"),
        "shell": "/bin/bash", "env": _env(project, v),
        "limits": {k: getattr(project.limits, k) for k in _SPEC_LIMITS},
        "start_at": {"stage": stages[0].name, "checkpoint": None},
        "stages": [_stage_spec(project, s, tasks, v) for s in stages],
    }
    if project.runtime.setup:
        spec["runtime"] = {"setup": config.render(project.runtime.setup, v),
                           "when_changed": list(project.runtime.when_changed)}
    return spec


# --- plan

def _reuse_row(db: Database, reuse: dict[str, object], need_tree: bool = True) -> dict[str, Any]:
    if "run_id" in reuse:
        row = db.run(db.resolve(str(reuse["run_id"])))
    else:
        rows = [r for r in db.runs() if r["label"] == reuse["label"]]
        row = rows[-1] if rows else None
    if row is None or (need_tree and not (row.get("root") and row.get("host"))):
        raise KeyError(f"reuse {reuse}: no run{' with a host and a root' if need_tree else ''} in the database")
    return row


def _fresh(job: Job) -> bool:
    """A job placed on a new tree: no reuse, or a reuse that restores from the archive."""
    return not job.reuse or bool(job.reuse.get("restore"))


def _check_overrides(project: Project, job: Job, names: list[str]) -> list[str]:
    bad = [k for k in job.overrides if not _ID_RE.match(k)]
    problems = [f"override key {k!r} is not a KEY=VALUE name" for k in bad]
    texts = [getattr(project.stages[n], key) for n in names for key in ("cmd", "resume", "prepare")]
    if job.overrides and not any("{overrides}" in t for t in texts):
        problems.append("overrides given, but no stage of the job uses {overrides}")
    return problems


def _plan_job(project: Project, batch: Batch, job: Job, db: Database, date: str,
              probes: dict[str, hosts.HostProbe], errors: dict[str, str], placed: dict[str, str | None]) -> RunPlan:
    names = job.stages or list(project.stages)
    sched = project.site.scheduler.backend in SCHEDULERS
    problems = _check_overrides(project, job, names)
    src, host, mount, root, reused, tag, tree = batch.source, None, "", "", "", "", ""
    restore = str(job.reuse.get("restore") or "") if job.reuse else ""
    if job.reuse:
        try:
            row = _reuse_row(db, job.reuse, need_tree=not restore)
            src, reused = row["src"], row["run_id"]
            # The tag and the tree id belong to the tree, through any chain of reuse.
            tag = row.get("build_tag") or ""
            tree = row.get("tree_id") or reused
            if not restore:
                host, root = row["host"], row["root"]
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
                            build_tag=tag, src=src, overrides=job.overrides, vars=job.vars)
    run_id = render_run_id(project.source.run_id, v)
    if _fresh(job) and sched:
        # The scheduler picks the host, and the tree lies where every node reads it.
        host = None if job.host == "auto" else job.host
        mount = os.path.expanduser(config.render(project.site.scheduler.tree_root, v))
        root = f"{mount}/{config.render(project.run_prefix, v)}/{run_id}"
    elif _fresh(job):
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
    tools = hosts.job_tools(project, job)
    lack = [] if sched else hosts.missing_tools(project.site, host, tools) if host else \
        [t for t in sorted(tools) if not any(h.has(t) for h in project.site.hosts.values())]
    if lack:
        problems.append((f"{host} lacks" if host else "no host has") + " the tools: " + ", ".join(lack))
    # {tree_id} names the tree the flow writes in: the reused run's id, else this run's.
    v.update(run_id=run_id, tree_id=tree or run_id, host=host, mount=mount, root=root,
             **hosts.tool_versions(project.site, host))
    spec: dict[str, Any] = {}
    if (host or (sched and root)) and not problems:
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
                   queued=not spec and not problems, problems=problems, src=src, build_tag=tag,
                   tree_host="local" if sched and _fresh(job) else host or "", reuse=reused, restore=restore, values=v)


def plan(project: Project, batch: Batch, ssh: hosts.Ssh, db: Database, date: str | None = None,
         only: list[str] | None = None, probes: dict[str, hosts.HostProbe] | None = None,
         backend: Backend | None = None) -> list[RunPlan]:
    """Render every job of a batch into a RunPlan; a problem is reported in the plan, not raised.

    `probes` are the results of a caller who probed already; the hosts are then not probed again.
    """
    date = date or pin_date(project.state_dir, batch.batch, dry_run=True)
    jobs = [j for j in batch.jobs if not only or j.label in only]
    if project.site.scheduler.backend in SCHEDULERS:
        return _slots(project, db, [_plan_job(project, batch, j, db, date, {}, {}, {}) for j in jobs])
    wanted = {j.host for j in jobs if _fresh(j)}
    names = (set(project.site.hosts) if "auto" in wanted else set()) | (wanted - {"auto"})
    errors: dict[str, str] = {}
    if probes is None:
        probes = {}
        for h, p in ((backend or make_backend(project.site, ssh)).free(sorted(names)) or {}).items():
            if isinstance(p, str):
                errors[h] = p
            else:
                probes[h] = p
    else:
        errors = {h: "no probe of the host" for h in names if h not in probes}
    auto = [j for j in jobs if j.host == "auto" and _fresh(j)]
    placed = hosts.place(project, auto, probes, {h: p.our_runs for h, p in probes.items()}) if auto else {}
    return [_plan_job(project, batch, j, db, date, probes, errors, placed) for j in jobs]


def _slots(project: Project, db: Database, plans: list[RunPlan]) -> list[RunPlan]:
    """Queue the plans above `[scheduler] max_jobs`, less the runs of the project the scheduler holds now."""
    sched = project.site.scheduler
    if not sched.max_jobs:
        return plans
    held = sum(1 for r in db.runs() if str(r.get("handle") or "").startswith(f"{sched.backend}:") and board.is_live(r))
    free = sched.max_jobs - held
    for p in plans:
        if p.spec and not p.problems:
            if free <= 0:
                p.spec, p.queued = {}, True
            free -= 1
    return plans


def request(project: Project, run_id: str, host: str | None, spec_path: Path, driver: Path) -> Request:
    """What the run of a written spec asks of the backend, over the stages the spec lists."""
    spec = config.load_json(spec_path)
    stages = [project.stages[s["name"]] for s in spec.get("stages") or [] if s.get("name") in project.stages]
    licences: dict[str, int] = {}
    for st in stages:
        for tool, seats in st.needs.tools.items():
            if name := project.site.tools[tool].licence:
                licences[name] = max(licences.get(name, 0), seats)
    # A task group runs `parallel` tasks at once, each with the cores of the stage.
    cores = max((st.needs.cores * (st.parallel if st.is_group else 1) for st in stages), default=1)
    timed = bool(stages) and all(st.budget.hours and st.budget.per == "stage" for st in stages)
    sched = project.site.scheduler
    return Request(run_id=run_id, spec=spec_path, driver=Path(driver), log=spec_path.with_name(f"{run_id}.driver.log"),
                   host=host, env=dict(project.site.env), project=project.project, cores=cores,
                   ram_gb=max((st.needs.ram_gb for st in stages), default=0.0),
                   disk_gb=stages[0].needs.disk_gb if stages else 0.0,
                   hours=sum(st.budget.hours or 0 for st in stages) if timed else None,
                   licences=licences, queue=sched.queue, options=list(sched.options))


# --- launch

def write_spec(state: Path, batch: str, plan_: RunPlan, driver: Path, dry_run: bool = False) -> Path:
    """Write <state_dir>/<batch>/<run_id>.spec.json by a temporary file and rename.

    The spec records `driver` and a `record` of what made the run: the edarunner version, the sha256
    of the driver, and the version of each tool the site file gives for the host.
    """
    plan_.spec["driver"] = str(driver)
    plan_.spec["record"] = {
        "edarunner": __version__,
        "driver_sha256": hashlib.sha256(DRIVER_SRC.read_bytes()).hexdigest(),
        "tools": {k[5:-8]: v for k, v in plan_.values.items() if k.startswith("tool.") and k.endswith(".version") and v},
    }
    path = Path(state) / batch / f"{plan_.run_id}.spec.json"
    if not dry_run:
        for st in plan_.spec.get("stages") or []:
            for t in [*(st.get("tools") or []), *(x for task in st.get("tasks") or [] for x in task.get("tools") or [])]:
                Path(t["leases"]).mkdir(parents=True, exist_ok=True)
        config.save_json(path, plan_.spec)
    return path


def submit(backend: Backend, db: Database, project: Project, run_id: str, host: str | None, spec_path: Path,
           driver: Path) -> Handle:
    """Start the driver of a written spec through the backend and record the handle in `runs`."""
    handle = backend.submit(request(project, run_id, host, spec_path, driver))
    db.upsert_run({"run_id": run_id, "handle": str(handle)})
    return handle


def _src_dir(project: Project, src: str, src_dir: Path | None, dry_run: bool = False) -> Path:
    if src_dir is not None:
        return Path(src_dir)
    try:
        from . import checkout
    except ImportError:
        raise Refuse("no src_dir given and checkout is missing") from None
    try:
        return Path(checkout.find(project, src))
    except checkout.CheckoutError:
        # A dry run checked nothing out; the tree would be at the path of the checkout.
        if dry_run and checkout.SRC_RE.match(src):
            return project.source.worktrees / src
        raise


def launch(project: Project, batch: Batch, ssh: hosts.Ssh, db: Database, dry_run: bool = False,
           only: list[str] | None = None, allow_dirty: bool = False, stagger_s: int | None = None,
           src_dir: Path | None = None, backend: Backend | None = None) -> list[dict[str, Any]]:
    """Pin the date, publish the driver, sync, write the specs and start one driver per run."""
    if "-dirty" in batch.source and not allow_dirty:
        raise Refuse(f"source {batch.source} is dirty; pass --allow-dirty")
    backend = backend or make_backend(project.site, ssh)
    state = project.state_dir
    date = pin_date(state, batch.batch, dry_run)
    plans = plan(project, batch, ssh, db, date=date, only=only, backend=backend)
    driver = sync.publish_driver(state, DRIVER_SRC, dry_run)
    if not dry_run:
        db.upsert_batch({"batch": batch.batch, "project": project.project, "source": batch.source, "run_date": date})
    stagger = project.limits.stagger_s if stagger_s is None else stagger_s
    out: list[dict[str, Any]] = []
    started = 0
    for p in plans:
        row = {"run_id": p.run_id, "label": p.label, "host": p.host, "root": p.root, "queued": p.queued,
               "problems": p.problems, "started": False, "pid": None}
        out.append(row)
        spec_path = state / batch.batch / f"{p.run_id}.spec.json"
        if p.spec and not p.problems and spec_path.exists():
            p.problems.append(f"already launched: {spec_path} exists")
        if p.queued and not dry_run:
            db.upsert_run({**_run_row(p, batch), "state": "queued"})
        if p.queued or p.problems:
            print(f"{p.run_id}: " + ("queued" if p.queued else "; ".join(p.problems)))
            continue
        if started and stagger > 0:
            time.sleep(stagger)
        if not p.reuse or p.restore:
            ok = sync.sync_tree(ssh, p.tree_host, _src_dir(project, p.src, src_dir, dry_run), p.root, project.sync.exclude,
                                project.safety.marker, project.safety.min_depth, dry_run)
            if ok and p.restore:
                res = collect.restore_on_request(project, ssh, db, db.run(p.reuse) or {}, p.restore,
                                                 p.tree_host, p.root, dry_run, p.spec.get("vars"))
                print(f"{p.run_id}: restore {p.restore} of {p.reuse}: {res.files} files" + (" (dry)" if dry_run else ""))
                for f in res.failures:
                    print(f"{p.run_id}: {f}")
                ok = not res.failures
            if ok and project.sync.after and not dry_run:
                ok = sync.run_after_hook(project.site, project, project.sync.after, p.values)
            if not ok:
                p.problems.append("sync failed")
                continue
        path = write_spec(state, batch.batch, p, driver, dry_run)
        print(f"{p.run_id}: {p.host or backend.name} {p.root}" + (" (dry)" if dry_run else ""))
        if dry_run:
            continue
        db.upsert_run({**_run_row(p, batch), "phase": "setup", "state": "running", "started": int(time.time())})
        db.add_event("user", p.run_id, "launch", f"{p.host or backend.name} {p.root}")
        try:
            handle = submit(backend, db, project, p.run_id, p.host, path, driver)
        except hosts.HostError as e:
            # A run the backend refused never starts; a live phase would wait for a heartbeat for ever.
            p.problems.append(f"submit failed: {e}")
            db.upsert_run({"run_id": p.run_id, "phase": "FAILED:submit", "state": "failed"})
            db.add_event("user", p.run_id, "launch", f"submit failed: {e}")
            print(f"{p.run_id}: submit failed: {e}")
            continue
        row["pid"], row["handle"] = handle.pid, str(handle)
        row["started"] = True
        started += 1
    return out


def _run_row(p: RunPlan, batch: Batch) -> dict[str, Any]:
    return {"run_id": p.run_id, "batch": batch.batch, "label": p.label, "config": p.spec.get("config") or p.values.get("config"),
            "build_tag": p.build_tag, "src": p.src, "dirty": int("-dirty" in p.src), "host": p.host, "root": p.root or None,
            "tree_id": p.values.get("tree_id") or p.run_id, "created": int(time.time())}


# --- stop

def stop(ssh: hosts.Ssh, db: Database, run_row: dict[str, Any], heartbeat: dict[str, Any], after_task: bool = False,
         now: bool = False, grace_s: int = 30, dry_run: bool = False, why: str = "", state: Path | None = None,
         actor: str = "user", backend: Backend | None = None) -> bool:
    """Stop one run: the stop file with `after_task`, else TERM to the driver and every pgid, KILL with `now`."""
    run_id, host = run_row["run_id"], run_row["host"]
    if after_task:
        if state is None:
            raise ValueError("after_task needs the state directory")
        path = Path(state) / run_row["batch"] / f"{run_id}.stop"
        print(f"{run_id}: write {path}" + (" (dry)" if dry_run else ""))
        if not dry_run:
            path.write_text("after-task\n")
            db.add_event(actor, run_id, "stop", f"after-task: {why}")
        return True
    pid, pgids = heartbeat.get("driver_pid"), [g for g in heartbeat.get("pgids") or [] if g]
    backend = backend or make_backend(ssh.site, ssh)
    # A scheduler stops its job by id, also before the driver wrote a heartbeat.
    job = run_handle(backend.name, run_row, heartbeat) if backend.name in SCHEDULERS else None
    if pid is None and not pgids and job is None:
        raise Refuse(f"{run_id}: no driver pid in the heartbeat; nothing to signal")
    print(f"{run_id}: " + (f"stop {job}" if job else f"TERM driver {pid} and pgids {pgids} on {host}")
          + (" (dry)" if dry_run else ""))
    if dry_run:
        return True
    if job is None:
        check_pid(pid)
    handle = job or Handle(backend.name, f"{host}:{pid if pid is not None else ''}", host)
    # The driver's own handler kills its groups and writes KILLED; the pgids follow for a dead driver.
    backend.stop(handle, False, pgids)
    end = time.time() + grace_s
    alive = pid is not None or job is not None
    while alive and time.time() < end:
        time.sleep(2)
        alive = not gone(backend, handle)
    if alive and now:
        backend.stop(handle, True, pgids)
        alive = not gone(backend, handle)
    db.add_event(actor, run_id, "stop", f"{why} [driver {pid}, pgids {pgids}, {'killed' if now else 'term'}, "
                     f"{'alive' if alive else 'ended'}]")
    return not alive


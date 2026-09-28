"""The watcher: one cycle over every heartbeat of one project.

The cycle reads, classifies, acts, collects, resumes, launches and writes
the boards. It never deletes a tree. Its memory between cycles is two rows
of the database's store table: progress (what each run looked like last
time) and notified (states, alerts, grace clocks). The watcher that holds
`serve.lock` also does the work of the user after its cycle; `census.py`
has it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from . import analysis, board, census, collect, config, home, hosts, launch, metrics
from .backend import Backend, Live, make_backend, run_handle
from .db import Database
from .guards import Refuse
from .hosts import HostError, Ssh
from .model import SCHEDULERS, Project, Task
from .notify import Notifier, alerts
from .notify.telegram import format as tgfmt

log = logging.getLogger(__name__)
Row = dict[str, Any]

_RUN_KEYS = ("run_id", "label", "config", "host", "root", "phase", "stage", "step", "exit", "killed_by",
             "started", "updated", "disk_free_gb", "tree_gb", "counts")
_TASK_KEYS = ("started", "ended", "exit", "signature", "log")
_BUSY = ("stage:", "group:", "retry:")


@dataclass(frozen=True)
class State:
    """One run state: the test that finds it, whether it alerts, and what the watcher does after `grace_s`.

    The state is the watcher's verdict on a run; the phase is the driver's word in the heartbeat for
    where the run is or how it ended. A live run gets its state from the heartbeat and the database;
    a finished run from its phase.
    Every change of state writes an event. A state that alerts sends one alert per run and reason,
    and a new reason edits that alert in place. A keep of N hours holds off the kill of `hung` and
    the stop of `superseded` for N hours; the full-host stop never waits for a keep.
    """

    test: str
    action: str
    alert: bool = False


STATES = {
    "running": State("the heartbeat is younger than `stale_s`", "none"),
    "stale": State("the heartbeat is older than `stale_s`; or older than `dead_s`, but the driver is alive or "
                   "the host did not answer", "none"),
    "dead": State("the heartbeat is older than `dead_s` and the driver process is gone from the host",
                  "one resume from the last step, when the stage has `resume` and no process group of the run "
                  "is alive", alert=True),
    "hung": State("the heartbeat is fresh, a stage or task runs, and nothing changed for `hung_s`: phase, step, "
                  "tree size, log tail, task counts, log size, CPU time of the process groups",
                  "`SIGTERM` to the process groups, only with `kill_hung` and while no keep holds", alert=True),
    "looping": State("the driver set `looping`: `streak` equal failure signatures in a row", "none", alert=True),
    "over_budget": State("the driver set `over_budget`, or the run ended `OVER_BUDGET`", "none", alert=True),
    "host_full": State("the driver set `host_full`: the free scratch is below the floor of the host",
                       "`stop --now` on the newest run of the host, of any of your projects, once per `grace_s` "
                       "while the host stays full; a keep does not hold it off", alert=True),
    "superseded": State("a newer batch runs the same label at another source",
                        "`stop --after-task`, while no keep holds", alert=True),
    "orphan": State("a process of the current user that matches `tool_procs` and that no live run owns: its "
                    "`EDR_RUN_ID` names a run of a registered project that ended or whose driver is gone, or an "
                    "unknown run whose tree `/<project>/<run_id>` holds the process, or it has no `EDR_RUN_ID` and "
                    "no safety marker of a registered project in its cwd or command line",
                    "`SIGTERM`, only with `kill_orphan` of the project the process belongs to", alert=True),
    "queued": State("no host fits the job, or the scheduler holds `max_jobs` runs of the project",
                    "a launch when a host fits or a job ends, one per batch per cycle"),
    "pending": State("the scheduler has the job in its queue and the driver has not started", "none"),
    "held": State("the scheduler holds the job and runs it only after a person releases it", "none", alert=True),
    "suspended": State("the scheduler suspended the job; the heartbeat stands still until it resumes", "none"),
    "imported": State("`edr import` recorded the run", "none"),
    "done": State("the run ended `done`", "none"),
    "incomplete": State("the run ended `INCOMPLETE`: a task failed or was skipped", "none", alert=True),
    "failed": State("the run ended `FAILED`", "none", alert=True),
    "stopped": State("a stop file or `edr stop` ended the run, or `edr stop` marked a queued run", "none"),
    "killed": State("a signal ended the run", "none", alert=True),
    "abandoned": State("`edr retire` took a live run", "none"),
    "retired": State("`edr retire` took the run", "none"),
}
_NOTIFY = frozenset(s for s, st in STATES.items() if st.alert)


# files

def _hb(project: Project, run: Row) -> dict:
    return config.load_json(project.state_dir / str(run.get("batch")) / f"{run['run_id']}.json")


def read_heartbeats(project: Project, batches: set[str] | None = None) -> list[tuple[str, dict]]:
    """Every heartbeat of every batch without RETIRED (or of `batches`), as (batch, heartbeat)."""
    out = []
    for bdir in sorted(p for p in project.state_dir.glob("*") if p.is_dir() and p.name != "bin"):
        if (bdir / "RETIRED").exists() or (batches is not None and bdir.name not in batches):
            continue
        for f in sorted(bdir.glob("*.json")):
            if not f.name.endswith((".spec.json", ".keep.json")):
                hb = config.load_json(f)
                if hb.get("run_id"):
                    out.append((bdir.name, hb))
    return out


def _stages(hb: dict) -> dict[str, dict]:
    """The stage entries of a heartbeat; a stage still running in a finished run ended with the run."""
    stage = hb.get("stage") or ""
    stages = hb.get("stages") or ({stage: {"log": hb.get("log")}} if stage else {})
    out = {}
    for name, s in stages.items():
        status = s.get("status") or "running"
        if status == "running" and not board.is_live(hb):
            status = board.state_of(hb)  # killed, stopped or failed
        out[name] = {**s, "status": status}
    return out


def ingest(db: Database, heartbeats: list[tuple[str, dict]]) -> None:
    """Upsert `runs` and `stage_runs` from the heartbeats; `state` is left to classify."""
    for batch, hb in heartbeats:
        db.upsert_run({"batch": batch, **{k: hb.get(k) for k in _RUN_KEYS}})
        for name, s in _stages(hb).items():
            db.upsert_stage_run({"run_id": hb["run_id"], "stage": name, "attempt": s.get("attempt") or 1,
                                     "status": s["status"], "started": s.get("started"), "ended": s.get("ended"),
                                     "exit": s.get("exit"), "log": s.get("log")})
        if hb.get("step_times"):
            db.set_step_times(hb["run_id"], hb["step_times"])
        db.add_run_sample(hb)
        for tid, t in (hb.get("tasks") or {}).items():
            db.upsert_stage_run({"run_id": hb["run_id"], "stage": hb.get("stage") or "", "task": tid, "status": t.get("phase"),
                                     **{k: t.get(k) for k in _TASK_KEYS}})


# classify

def _signature(hb: dict) -> list:
    """What the hung check compares between cycles."""
    counts = hb.get("counts") or {}
    return [hb.get("phase"), hb.get("step"), hb.get("tree_gb"), hb.get("last_log"),
            sum(counts.get(k, 0) for k in ("done", "failed", "skipped")), hb.get("log_bytes"), hb.get("cpu_s")]


def classify(project: Project, ssh: Ssh, db: Database, run: Row, heartbeat: dict, now: float,
             progress: dict | None = None, live: Live | None = None) -> tuple[str, list[str]]:
    """The state of one run (running, stale, dead, hung, looping, over_budget, host_full or superseded) and every reason found.

    `live` is what the backend said of the driver this cycle; None means the run has no handle."""
    hb, lim = heartbeat, project.limits
    if not board.is_live(hb):
        return board.state_of(hb), []
    if live is Live.HELD:
        return "held", ["held by the scheduler"]
    if live is Live.SUSPENDED:
        return "suspended", ["suspended by the scheduler"]
    age, host, pid = now - (hb.get("updated") or 0), hb.get("host") or run.get("host"), hb.get("driver_pid")
    # A reason text stays the same from cycle to cycle, or every cycle would re-send the alert.
    if age > lim.dead_s:
        if live is Live.UNKNOWN:
            return "stale", [f"heartbeat older than {lim.dead_s} s, {host} did not answer"]
        if live in (None, Live.GONE):
            return "dead", [f"heartbeat older than {lim.dead_s} s, driver {pid} gone on {host}"]
        return "stale", [f"heartbeat older than {lim.dead_s} s, driver {pid} alive"]
    if age > lim.stale_s:
        return "stale", [f"heartbeat older than {lim.stale_s} s"]
    found = []
    if hb.get("looping"):
        found.append(("looping", "driver reported it"))
    if hb.get("over_budget"):
        found.append(("over_budget", f"stage {hb['over_budget']}"))
    if hb.get("host_full"):
        found.append(("host_full", f"{host} below {hosts.floor(project.site, str(host)):g} GB free"))
    busy = str(hb.get("phase") or "").startswith(_BUSY) and (hb.get("pgids") or (hb.get("counts") or {}).get("running"))
    if busy and progress is not None:
        rec, sig = progress.setdefault(hb["run_id"], {}), _signature(hb)
        if rec.get("sig") != sig:
            rec.update(sig=sig, since=now)
        elif now - rec["since"] >= lim.hung_s:
            found.append(("hung", "no progress since " + time.strftime("%d.%m %H:%M", time.localtime(rec["since"]))))
    newer = [r["run_id"] for r in db.runs() if r["label"] == run.get("label") and r["batch"] != run.get("batch")
             and r["run_id"] > run["run_id"] and r.get("source") != run.get("source") and r.get("phase") and board.is_live(r)]
    if newer:
        found.append(("superseded", f"by {max(newer)}"))
    if not found:
        return "running", []
    return found[0][0], [f"{s}: {r}" for s, r in found]


# actions

def _act(project: Project, ssh: Ssh, db: Database, run: Row, state: str, reasons: list[str], text: str,
         now: float, notes: dict) -> bool:
    """The kill or stop of one state; True when it ran or is settled, False to try again next cycle."""
    lim, host, run_id = project.limits, run.get("host"), run["run_id"]
    if config.kept(project, run, now):
        return False  # the keep holds off every action; the next cycle asks again
    if any(r.startswith("superseded:") for r in reasons):
        stop_path = project.state_dir / str(run.get("batch")) / f"{run_id}.stop"
        if not stop_path.exists():
            launch.stop(ssh, db, run, {}, after_task=True, why=text, state=project.state_dir, actor="watch")
        if state == "superseded":
            return True
    if state == "hung" and lim.kill_hung:
        pgids = _hb(project, run).get("pgids") or []
        for pgid in pgids:
            ssh.kill_pgid(host, pgid, "TERM")
        db.add_event("watch", run_id, "kill", f"hung: TERM pgids {pgids} on {host}")
    return True


def actions(project: Project, ssh: Ssh, db: Database, notifiers: list[Notifier], run: Row, state: str,
            reasons: list[str], now: float, notes: dict | None = None) -> None:
    """An event per (run, state) transition, one alert per (run, kind), and the kills after grace_s."""
    notes = {} if notes is None else notes
    run_id, text = run["run_id"], "; ".join(reasons) or state
    rec = notes.setdefault(run.get("key") or run_id, {})
    if rec.get("state") != state:
        if not (rec.get("state") is None and state == "running"):
            db.add_event("watch", run_id, state, text)
        rec.update(state=state, since=now, acted=False)
    if state in _NOTIFY:
        msgs = rec.setdefault("msgs", {})
        if (msgs.get(state) or {}).get("text") != text:
            # A repeat send edits the earlier message in place and keeps its buttons.
            alert = alerts.run_alert(project, run, state, reasons, _hb(project, run), now)
            ids = [n.send(alert) for n in notifiers]
            msgs[state] = {"text": text, "ids": [i for i in ids if i]}
    if rec.get("acted") or now - rec.get("since", now) < project.limits.grace_s:
        return
    try:
        rec["acted"] = _act(project, ssh, db, run, state, reasons, text, now, notes)
    except (HostError, Refuse, OSError, ValueError) as e:
        db.add_event("watch", run_id, "kill", f"{state}: failed: {e}")
        rec["acted"] = True


# collect, extract, resume, queue

def _parameters(project: Project, run: Row) -> dict[str, Any]:
    out = {k: run.get(k) for k in ("config", "build_tag", "source") if run.get(k)}
    try:
        job = next(j for j in config.load_batch(project, str(run["batch"])).jobs if j.label == run.get("label"))
        out.update(job.overrides)
    except (config.ConfigError, StopIteration):
        pass
    return out


def _collect(project: Project, ssh: Ssh, db: Database, run: Row, hb: dict, progress: dict, host: str = "") -> None:
    spec = collect.load_spec(project, run)
    if spec.get("collect") is False:  # edr track without --collect
        return
    only = collect.spec_stages(spec)
    finished, running = collect.stage_state(project, hb, only)
    tasks = {t: e.get("phase") for t, e in (hb.get("tasks") or {}).items() if e.get("phase") in ("done", "failed")}
    key = [finished, sorted(tasks), hb.get("step") if running else None, board.is_live(hb)]
    rec = progress.setdefault(run["run_id"], {})
    if rec.get("collected") == key or not (finished or tasks or running):
        return
    rec["collected"] = key
    _pulse(project)
    res = collect.collect_run(project, ssh, db, run, hb, host=host)
    if res.failures:
        db.add_event("watch", run["run_id"], "collect", f"{len(res.failures)} failed: {res.failures[0]}")
    # No new file does not mean no new metric: a file can arrive before its step or stage counts as
    # finished. Extraction is idempotent, so run it.
    rows = extract_run(project, db, run, hb, spec, tasks)
    new = [r for r in rows if r["value"] is not None and db.add_metric(r)]
    if new:
        db.add_event("watch", run["run_id"], "metrics", _added(new))
    if not rec.get("params"):
        db.set_parameters(run["run_id"], _parameters(project, run), "spec")
        rec["params"] = True
    log.info("%s: %d files, %d new metrics", run["run_id"], res.files, len(new))


def _added(rows: list[dict]) -> str:
    """The text of a metrics event, such as `6 new: area_cell_um2, wns_ns at pnr 8, 9`."""
    at = []
    for stage in dict.fromkeys(r["stage"] for r in rows):
        steps = sorted({r["step"] for r in rows if r["stage"] == stage and r["step"] is not None})
        tasks = sorted({r["task"] for r in rows if r["stage"] == stage and r["task"]})
        at.append(" ".join([stage, ", ".join(map(str, steps + tasks))]).rstrip())
    return f"{len(rows)} new: {', '.join(sorted({r['name'] for r in rows}))} at {'; '.join(at)}"


def extract_run(project: Project, db: Database, run: Row, hb: dict, spec: dict | None = None,
                tasks: dict[str, str] | None = None, actor: str | None = "watch") -> list[dict]:
    """The metric rows of a run from its collected files: the done tasks, the stages that exited 0, and
    the finished steps of every other stage.

    A step is finished when `step_runs` holds it and a later step of the run, whatever the status of
    its stage. `tasks` maps a task id to its phase, by default from the heartbeat. A run without a
    heartbeat, such as an imported one, takes every stage and the tasks its metric rows already
    name. An unknown task is an event of `actor`; None writes no event.
    """
    spec = collect.load_spec(project, run) if spec is None else spec
    only = collect.spec_stages(spec)
    if tasks is None:
        tasks = {t: e.get("phase") for t, e in (hb.get("tasks") or {}).items()}
    if not hb:
        tasks = {m["task"]: "done" for m in db.metrics(run_ids=[run["run_id"]]) if m["task"]}
    task_dirs = collect.spec_task_dirs(spec, str(run.get("root") or hb.get("root") or ""))
    done: dict[str, Task] = {}
    for t, p in tasks.items():
        if p != "done":
            continue
        try:
            done[t] = config.resolve_task(project, t)
        except config.ConfigError as e:
            # The spec names the task's directory even when the task table no longer has it.
            if t in task_dirs:
                done[t] = Task(id=t, fields={"id": t})
            elif actor:
                db.add_event(actor, run["run_id"], "extract", f"unknown task {t}: {e}")
    # A stage that exited 0 (done, or over budget without a kill) ran all its steps, and a task group
    # counts per task with phase done. Any other stage keeps its finished steps only: a copied tree
    # can hold a stale report of a step this run never started.
    status = _stages(hb)
    whole = {n for n, st in project.stages.items() if st.is_group or status.get(n, {}).get("status") == "done"
             or status.get(n, {}).get("exit") == 0}
    finished = set(sorted({r["step"] for r in db.step_runs(run["run_id"])})[:-1])
    eligible = {n for n, st in project.stages.items() if (only is None or n in only) and (n in whole or st.steps)}
    rows = metrics.extract(project, run, project.data / "results", done, stages=eligible if hb else None,
                           task_dirs=task_dirs)
    return [r for r in rows if not hb or r["stage"] in whole or r["step"] in finished]


def _resume(project: Project, ssh: Ssh, backend: Backend, db: Database, run: Row, hb: dict, progress: dict,
            now: float) -> None:
    rec, run_id = progress.setdefault(run["run_id"], {}), run["run_id"]
    spec_path = project.state_dir / str(run["batch"]) / f"{run_id}.spec.json"
    spec = config.load_json(spec_path)
    stage = next((s for s in spec.get("stages") or [] if s.get("name") == hb.get("stage")), None)
    driver = spec.get("driver")
    if rec.get("resumed") or not stage or not stage.get("resume") or not driver:
        return
    sched = backend.name in SCHEDULERS
    # A scheduler ends the process family of its job, and the tree under tree_root is read on any node.
    host = None if sched else str(run["host"])
    alive = [] if sched else [g for g in hb.get("pgids") or [] if int(g) > 1
                              and ssh.run(str(host), f"kill -0 -- -{int(g)}")[0] == 0]
    if alive:
        # The tool outlived its driver; a resume now would write the same tree twice.
        if not rec.get("resume_wait"):
            rec["resume_wait"] = True
            db.add_event("watch", run_id, "resume", f"deferred: pgids {alive} alive on {host}")
        return
    rec["resumed"] = True
    checkpoint = hb.get("step_name") if hb.get("step") else None
    spec["start_at"] = {"stage": stage["name"], "checkpoint": checkpoint}
    config.save_json(spec_path, spec)
    try:
        handle = launch.submit(backend, db, project, run_id, host, spec_path, driver)
    except (HostError, OSError) as e:
        db.add_event("watch", run_id, "resume", f"failed: {e}")
        return
    db.upsert_stage_run({"run_id": run_id, "stage": stage["name"], "attempt": 2, "started": int(now),
                             "status": "resumed", "log": str(spec_path.with_name(f"{run_id}.driver.log"))})
    db.add_event("watch", run_id, "resume", f"{stage['name']} from {checkpoint or 'start'}, driver {handle}")


def _launch_queued(project: Project, ssh: Ssh, db: Database) -> None:
    queued: dict[str, list[Row]] = {}
    for r in db.runs(state="queued"):
        queued.setdefault(str(r["batch"]), []).append(r)
    for name, rows in queued.items():
        _pulse(project)
        try:
            batch = config.load_batch(project, name)
            # The batch file may name a ref; the queued run keeps the source tag, and so its run id.
            batch.source = rows[0]["source"] or batch.source
            launch.launch(project, batch, ssh, db, only=[str(rows[0]["label"])], stagger_s=0, allow_dirty=True)
        except Exception:  # one bad batch must not end the watcher
            log.exception("queued batch %s", name)


# boards and the cycle

def _pulse(project: Project) -> None:
    """Show the supervisor that this watcher lives, before a collect or a launch that may take long."""
    path = project.state_dir / "watch.json"
    config.save_json(path, {**config.load_json(path), "ts": time.time(), "pid": os.getpid()})


def _boards(project: Project, backend: Backend, db: Database, notifiers: list[Notifier], now: float,
            pin: bool = True) -> None:
    # The census of the last two cycles saves an ssh call per host.
    probes = census.probes(project, 2 * project.limits.heartbeat_s)
    if probes is None:
        probes = {h: {"error": p} if isinstance(p, str) else asdict(p) for h, p in (backend.free(project.site.hosts) or {}).items()}
    bdir = project.data / "board"
    db.add_host_samples(int(now), probes)
    db.write_board_json(bdir / "board.json", probes)
    retired = {b["batch"] for b in db.batches() if b.get("retired")}
    rows = [r for r in db.runs() if r["batch"] not in retired]
    config.save_text(bdir / "status.html", board.status_html(rows, db.events(n=50), probes, now,
                                                           db.host_samples(int(now) - 86400)))
    parameters = db.parameters()
    plotly = board.PLOTLY_FILE if (bdir / board.PLOTLY_FILE).is_file() else board.PLOTLY_URL
    areas = analysis.last_areas(db, [r["run_id"] for r in rows])
    config.save_text(bdir / "compare.html", board.compare_html(rows, parameters, db.metrics(), plotly, areas,
                                                                analysis.step_names(project)))
    text = tgfmt.board(rows, now=now, totals=metrics.step_totals(project), names=analysis.step_names(project))
    for n in notifiers if pin else []:
        n.board(text)


def _waiting(name: str, db: Database, seen: set[str]) -> dict[str, Any]:
    """The live runs in a scheduler that wrote no heartbeat yet, with their handles."""
    if name not in SCHEDULERS:
        return {}
    out = {}
    for r in db.runs():
        if r["run_id"] not in seen and board.is_live(r) and r.get("state") not in ("queued", "retired", "abandoned"):
            if h := run_handle(name, r, {}):
                out[r["run_id"]] = h
    return out


def _unstarted(db: Database, run_id: str, live: Live, why: str, dry_run: bool) -> tuple[str, list[str]]:
    """The state of a scheduler job before its first heartbeat; a job that left the queue failed."""
    if live is Live.GONE:
        if not dry_run:
            db.upsert_run({"run_id": run_id, "phase": "FAILED:scheduler", "exit": None})
        return "failed", [f"the job left the scheduler before the driver started: {why}"]
    state = {Live.PENDING: "pending", Live.HELD: "held", Live.SUSPENDED: "suspended", Live.RUNNING: "running"}.get(live, "stale")
    if not dry_run:
        db.upsert_run({"run_id": run_id, "state": state})
    return state, [why] if why and state != "running" else []


def cycle(project: Project, ssh: Ssh, db: Database, notifiers: list[Notifier], now: float | None = None,
          dry_run: bool = False, backend: Backend | None = None, start: bool = True, served: bool = False) -> dict[str, str]:
    """One cycle; returns {run_id: state}. A dry run reads, classifies and prints, and writes nothing.

    The backend answers once per cycle for the drivers of every live run. Without `start` the cycle
    resumes and launches nothing. A `served` cycle leaves the pinned board to the supervisor."""
    now = time.time() if now is None else now
    backend = backend or make_backend(project.site, ssh)
    progress, notes = db.get_store("progress", {}), db.get_store("notified", {})
    heartbeats = read_heartbeats(project)
    if not dry_run:
        ingest(db, heartbeats)
    handles = {}
    for batch, hb in heartbeats:
        if board.is_live(hb) and (h := run_handle(backend.name, db.run(hb["run_id"]) or {}, hb)):
            handles[hb["run_id"]] = h
    waiting = _waiting(backend.name, db, {hb["run_id"] for _, hb in heartbeats})
    handles.update(waiting)
    alive = backend.alive(list(handles.values())) if handles else {}
    states: dict[str, str] = {}
    for run_id, h in waiting.items():
        state, reasons = _unstarted(db, run_id, *alive[h], dry_run)
        states[run_id] = state
        if dry_run:
            print(f"{run_id}: {state} {'; '.join(reasons)}".rstrip())
        else:
            actions(project, ssh, db, notifiers, db.run(run_id) or {"run_id": run_id}, state, reasons, now, notes)
    for batch, hb in heartbeats:
        run = db.run(hb["run_id"]) or {**hb, "batch": batch}
        h = handles.get(hb["run_id"])
        live = alive[h][0] if h in alive else None
        if run.get("state") in ("retired", "abandoned"):
            states[hb["run_id"]] = run["state"]
            continue
        state, reasons = classify(project, ssh, db, run, hb, now, progress, live)
        states[hb["run_id"]] = state
        if dry_run:
            print(f"{hb['run_id']}: {state} {'; '.join(reasons)}".rstrip())
            continue
        db.upsert_run({"run_id": hb["run_id"], "state": state})
        actions(project, ssh, db, notifiers, run, state, reasons, now, notes)
        _collect(project, ssh, db, run, hb, progress, backend.file_host(run))
        if state == "dead" and start:
            _resume(project, ssh, backend, db, run, hb, progress, now)
    if dry_run:
        return states
    if start:
        _launch_queued(project, ssh, db)
    _boards(project, backend, db, notifiers, now, pin=not served)
    db.set_store("progress", progress)
    db.set_store("notified", notes)
    n = int(config.load_json(project.state_dir / "watch.json").get("cycle") or 0) + 1
    config.save_json(project.state_dir / "watch.json", {"ts": now, "cycle": n, "pid": os.getpid()})
    return states


def run_forever(project: Project, ssh: Ssh, db: Database, notifiers: list[Notifier], once: bool = False,
                served: bool = False) -> int:
    """A cycle every limits.heartbeat_s. With `once`: 1 when the cycle failed.

    A config that stops loading gets one alert per error text, and the cycles go on with the last one
    that loaded and start nothing until the file loads again. Unless the supervisor started it
    (`served`), the first watcher that takes `serve.lock` keeps it, starts to take the commands of
    the bot, and does the work of the user after each cycle."""
    failed, broken, serve = False, "", None
    try:
        while True:
            t0 = time.time()
            try:
                # A watcher runs for weeks; the config it read at start must not rule every cycle.
                try:
                    project, broken = config.load_project(project.root), ""
                except config.ConfigError as e:
                    failed = True
                    if str(e) != broken:
                        broken = str(e)
                        log.error("config not loadable, watching on the last good one: %s", e)
                        for n in notifiers:
                            n.send(alerts.config_alert(project, broken))
                ssh = Ssh(project.site)
                for n in notifiers:
                    if hasattr(n, "project"):
                        n.project = project
                cycle(project, ssh, db, notifiers, backend=make_backend(project.site, ssh), start=not broken, served=served)
            except Exception:  # the next cycle sees a fresh state; the log keeps the traceback
                log.exception("watch cycle failed")
                failed = True
            if serve is None and not served and (serve := home.lock(home.root() / "serve.lock")) is not None:
                for n in notifiers:
                    n.start()  # the holder of serve.lock takes the commands of every project
            if serve is not None:
                try:
                    census.work(notifiers, own=project)
                except Exception:
                    log.exception("census failed")
                    failed = True
            if once:
                return int(failed)
            time.sleep(max(1.0, project.limits.heartbeat_s - (time.time() - t0)))
    finally:
        for n in notifiers:
            n.stop()
        if serve is not None:
            os.close(serve)


def check(project: Project, notifiers: list[Notifier] = ()) -> int:
    """1 (and an alert) when watch.json is older than three cycles, else 0."""
    w = config.load_json(project.state_dir / "watch.json")
    age = time.time() - float(w.get("ts") or 0)
    if age <= 3 * project.limits.heartbeat_s:
        return 0
    text = f"watch.json is {int(age)} s old (pid {w.get('pid')})" if w else "no watch.json"
    print(f"edr watch: {text}")
    alert = alerts.watch_alert(project, age if w else None, w.get("pid"))
    for n in notifiers:
        n.send(alert)
    return 1

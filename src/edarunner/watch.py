"""The watcher: one cycle over every heartbeat.

The cycle reads, classifies, acts, collects, resumes, launches and writes
the boards. It never deletes a file or a tree. Its memory between cycles
is two rows of the ledger's kv table: progress (what each run looked
like last time) and notified (states, alerts, grace clocks).
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import asdict
from typing import Any

from . import board, collect, config, launch, metrics
from .guards import Refuse
from .hosts import HostError, Ssh
from .ledger import Ledger
from .model import Project, Task
from .notify import Notifier, alert_buttons
from .notify.telegram import format as tgfmt

log = logging.getLogger(__name__)
Row = dict[str, Any]

_RUN_KEYS = ("run_id", "label", "config", "host", "root", "phase", "stage", "step", "exit", "killed_by",
             "started", "updated", "disk_free_gb", "tree_gb", "counts")
_TASK_KEYS = ("started", "ended", "exit", "signature", "log")
_BUSY = ("stage:", "group:", "retry:")
_NOTIFY = {"dead", "hung", "looping", "over_budget", "host_full", "superseded", "orphan", "incomplete", "failed",
           "killed"}


# files

def _hb(project: Project, run: Row) -> dict:
    return config.load_json(project.state / str(run.get("batch")) / f"{run['run_id']}.json")


def _keep(project: Project, run: Row) -> dict:
    return config.load_json(project.state / str(run.get("batch")) / f"{run['run_id']}.keep.json")


def read_heartbeats(project: Project, batches: set[str] | None = None) -> list[tuple[str, dict]]:
    """Every heartbeat of every batch without RETIRED (or of `batches`), as (batch, heartbeat)."""
    out = []
    for bdir in sorted(p for p in project.state.glob("*") if p.is_dir() and p.name != "bin"):
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


def ingest(ledger: Ledger, heartbeats: list[tuple[str, dict]]) -> None:
    """Upsert `runs` and `stage_runs` from the heartbeats; `state` is left to classify."""
    for batch, hb in heartbeats:
        ledger.upsert_run({"batch": batch, **{k: hb.get(k) for k in _RUN_KEYS}})
        for name, s in _stages(hb).items():
            ledger.upsert_stage_run({"run_id": hb["run_id"], "stage": name, "attempt": s.get("attempt") or 1,
                                     "status": s["status"], "started": s.get("started"), "ended": s.get("ended"),
                                     "exit": s.get("exit"), "log": s.get("log")})
        for tid, t in (hb.get("tasks") or {}).items():
            ledger.upsert_stage_run({"run_id": hb["run_id"], "stage": hb.get("stage") or "", "task": tid, "status": t.get("phase"),
                                     **{k: t.get(k) for k in _TASK_KEYS}})


# classify

def _signature(ssh: Ssh, hb: dict) -> list:
    counts = hb.get("counts") or {}
    sig = [hb.get("phase"), hb.get("step"), hb.get("tree_gb"), hb.get("last_log"),
           sum(counts.get(k, 0) for k in ("done", "failed", "skipped"))]
    pgids = " ".join(str(int(g)) for g in hb.get("pgids") or [])
    cmds = [f"stat -c %s {shlex.quote(str(hb['log']))} 2>/dev/null"] if hb.get("log") else []
    if pgids:
        cmds.append("ps -e -o pgid=,cputimes= | awk -v g=%s 'BEGIN{split(g,a,\" \");for(i in a)w[a[i]]=1}"
                    " ($1 in w){s+=$2} END{print s+0}'" % shlex.quote(pgids))
    if cmds and hb.get("host"):
        rc, out, _ = ssh.run(hb["host"], "; ".join(cmds))
        sig.append(out.split() if rc == 0 else None)
    return sig


def classify(project: Project, ssh: Ssh, ledger: Ledger, run: Row, heartbeat: dict, now: float,
             progress: dict | None = None) -> tuple[str, list[str]]:
    """The state of one run (running, stale, dead, hung, looping, over_budget, host_full or superseded) and every reason found."""
    hb, lim = heartbeat, project.limits
    if not board.is_live(hb):
        return board.state_of(hb), []
    age, host, pid = now - (hb.get("updated") or 0), hb.get("host") or run.get("host"), hb.get("driver_pid")
    # A reason text stays the same from cycle to cycle, or every cycle would re-send the alert.
    if age > lim.dead_s:
        try:
            alive = bool(pid) and ssh.pid_alive(host, int(pid))
        except HostError:
            return "stale", [f"heartbeat older than {lim.dead_s} s, {host} did not answer"]
        if not alive:
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
        found.append(("host_full", f"{host} below {lim.host_free_min_gb} GB free"))
    busy = str(hb.get("phase") or "").startswith(_BUSY) and (hb.get("pgids") or (hb.get("counts") or {}).get("running"))
    if busy and progress is not None:
        rec, sig = progress.setdefault(hb["run_id"], {}), _signature(ssh, hb)
        if rec.get("sig") != sig:
            rec.update(sig=sig, since=now)
        elif now - rec["since"] >= lim.hung_s:
            found.append(("hung", "no progress since " + time.strftime("%d.%m %H:%M", time.localtime(rec["since"]))))
    newer = [r["run_id"] for r in ledger.runs() if r["label"] == run.get("label") and r["batch"] != run.get("batch")
             and r["run_id"] > run["run_id"] and r.get("src") != run.get("src") and r.get("phase") and board.is_live(r)]
    if newer:
        found.append(("superseded", f"by {max(newer)}"))
    if not found:
        return "running", []
    return found[0][0], [f"{s}: {r}" for s, r in found]


# actions

def _newest_on(ledger: Ledger, host: str) -> Row | None:
    live = [r for r in ledger.runs() if r.get("host") == host and r.get("phase") and board.is_live(r)]
    return max(live, key=lambda r: r["run_id"], default=None)


def _act(project: Project, ssh: Ssh, ledger: Ledger, run: Row, state: str, reasons: list[str], text: str,
         now: float, notes: dict) -> bool:
    """The kill or stop of one state; True when it ran or is settled, False to try again next cycle."""
    lim, host, run_id = project.limits, run.get("host"), run["run_id"]
    if any(r.startswith("superseded:") for r in reasons):
        stop_path = project.state / str(run.get("batch")) / f"{run_id}.stop"
        if not _keep(project, run) and not stop_path.exists():
            launch.stop(ssh, ledger, run, {}, after_task=True, why=text, state=project.state, actor="watch")
        if state == "superseded":
            return True
    if state == "host_full":
        clocks, target = notes.setdefault("_hosts", {}), _newest_on(ledger, host)
        if target is None or now - clocks.get(host, 0) < lim.grace_s or _keep(project, target).get("ack"):
            return False
        clocks[host] = now
        launch.stop(ssh, ledger, target, _hb(project, target), now=True, why=f"{host} full: {text}", actor="watch")
        return True
    if state == "hung" and lim.kill_hung and not _keep(project, run).get("ack"):
        pgids = _hb(project, run).get("pgids") or []
        for pgid in pgids:
            ssh.kill_pgid(host, pgid, "TERM")
        ledger.add_event("watch", run_id, "kill", f"hung: TERM pgids {pgids} on {host}")
    if state == "orphan" and lim.kill_orphan:
        ssh.run(host, f"kill -TERM {int(run['pid'])}")
        ledger.add_event("watch", run_id, "kill", f"orphan: TERM pid {run['pid']} on {host}")
    return True


def actions(project: Project, ssh: Ssh, ledger: Ledger, notifiers: list[Notifier], run: Row, state: str,
            reasons: list[str], now: float, notes: dict | None = None) -> None:
    """An event per (run, state) transition, one alert per (run, kind), and the kills after grace_s."""
    notes = {} if notes is None else notes
    run_id, text = run["run_id"], "; ".join(reasons) or state
    rec = notes.setdefault(run.get("key") or run_id, {})
    if rec.get("state") != state:
        if not (rec.get("state") is None and state == "running"):
            ledger.add_event("watch", run_id, state, text)
        rec.update(state=state, since=now, acted=False)
    if state in _NOTIFY:
        msgs = rec.setdefault("msgs", {})
        if (msgs.get(state) or {}).get("text") != text:
            handle = board.handle(run)
            cmd = board.triage_cmd(run, state, _hb(project, run) if state == "dead" else {})
            # A repeat send edits the earlier message in place and keeps its buttons.
            reason = text if reasons else run.get("phase") or state
            ids = [n.send(state, run.get("key") or run_id, f"{state} {handle}\n{reason}",
                          alert_buttons(handle) if run_id else None, cmd) for n in notifiers]
            msgs[state] = {"text": text, "ids": [i for i in ids if i]}
    if rec.get("acted") or now - rec.get("since", now) < project.limits.grace_s:
        return
    try:
        rec["acted"] = _act(project, ssh, ledger, run, state, reasons, text, now, notes)
    except (HostError, Refuse, OSError, ValueError) as e:
        ledger.add_event("watch", run_id, "kill", f"{state}: failed: {e}")
        rec["acted"] = True


def orphans(project: Project, ssh: Ssh, ledger: Ledger) -> list[Row]:
    """Our tool processes on every site host that no live run root owns."""
    if not project.site.tool_procs:
        return []
    roots: dict[str, list[str]] = {}
    for r in ledger.runs():
        if r.get("root") and r.get("phase") and board.is_live(r):
            roots.setdefault(str(r.get("host")), []).append(r["root"])
    out = []
    for host in project.site.hosts:
        try:
            procs = ssh.tool_processes(host, project.site.tool_procs)
        except HostError as e:
            log.warning("%s: %s", host, e)
            continue
        pids = " ".join(str(p[0]) for p in procs)
        _, cwds, _ = ssh.run(host, f'for p in {pids}; do echo "$p $(readlink /proc/$p/cwd)"; done') if pids else (0, "", "")
        cwd = dict(line.split(" ", 1) for line in cwds.splitlines() if " " in line)
        for pid, etimes, _, comm, args in procs:
            own = roots.get(host, [])
            if not any(r in args or cwd.get(str(pid), "").startswith(r) for r in own):
                out.append({"key": f"orphan:{host}:{pid}", "run_id": "", "label": comm, "batch": host, "host": host,
                            "pid": pid, "phase": args, "etimes": etimes})
    return out


# collect, extract, resume, queue

def _params(project: Project, run: Row) -> dict[str, Any]:
    out = {k: run.get(k) for k in ("config", "build_tag", "src") if run.get(k)}
    try:
        job = next(j for j in config.load_batch(project, str(run["batch"])).jobs if j.label == run.get("label"))
        out.update(job.overrides)
    except (config.ConfigError, StopIteration):
        pass
    return out


def _collect(project: Project, ssh: Ssh, ledger: Ledger, run: Row, hb: dict, progress: dict) -> None:
    spec = collect.load_spec(project, run)
    only = collect.spec_stages(spec)
    finished, running = collect.stage_state(project, hb, only)
    tasks = {t: e.get("phase") for t, e in (hb.get("tasks") or {}).items() if e.get("phase") in ("done", "failed")}
    key = [finished, sorted(tasks), hb.get("step") if running else None, board.is_live(hb)]
    rec = progress.setdefault(run["run_id"], {})
    if rec.get("collected") == key or not (finished or tasks or running):
        return
    rec["collected"] = key
    res = collect.collect_run(project, ssh, ledger, run, hb)
    if res.failures:
        ledger.add_event("watch", run["run_id"], "collect", f"{len(res.failures)} failed: {res.failures[0]}")
    if not res.copied:
        return
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
            else:
                ledger.add_event("watch", run["run_id"], "extract", f"unknown task {t}: {e}")
    # A failed stage can leave a stale report from a copied tree: a one-command stage counts
    # only with status done, and a task group counts per task with phase done.
    status = _stages(hb)
    eligible = {n for n, st in project.stages.items() if (only is None or n in only)
                and (st.is_group or status.get(n, {}).get("status") == "done")}
    rows = metrics.extract(project, run, project.data / "results", done, stages=eligible, task_dirs=task_dirs)
    n = sum(ledger.add_metric(r) for r in rows if r["value"] is not None)
    if not rec.get("params"):
        ledger.set_params(run["run_id"], _params(project, run), "spec")
        rec["params"] = True
    log.info("%s: %d files, %d new metrics", run["run_id"], res.files, n)


def _resume(project: Project, ssh: Ssh, ledger: Ledger, run: Row, hb: dict, progress: dict, now: float) -> None:
    rec, run_id = progress.setdefault(run["run_id"], {}), run["run_id"]
    spec_path = project.state / str(run["batch"]) / f"{run_id}.spec.json"
    spec = config.load_json(spec_path)
    stage = next((s for s in spec.get("stages") or [] if s.get("name") == hb.get("stage")), None)
    driver = spec.get("driver")
    if rec.get("resumed") or not stage or not stage.get("resume") or not driver:
        return
    host = str(run["host"])
    alive = [g for g in hb.get("pgids") or [] if int(g) > 1 and ssh.run(host, f"kill -0 -- -{int(g)}")[0] == 0]
    if alive:
        # The tool outlived its driver; a resume now would write the same tree twice.
        if not rec.get("resume_wait"):
            rec["resume_wait"] = True
            ledger.add_event("watch", run_id, "resume", f"deferred: pgids {alive} alive on {host}")
        return
    rec["resumed"] = True
    checkpoint = hb.get("step_name") if hb.get("step") else None
    spec["start_at"] = {"stage": stage["name"], "checkpoint": checkpoint}
    config.save_json(spec_path, spec)
    logf = spec_path.with_name(f"{run_id}.driver.log")
    try:
        pid = launch.start_driver(ssh, host, driver, spec_path, logf, project.site.env)
    except (HostError, OSError) as e:
        ledger.add_event("watch", run_id, "resume", f"failed: {e}")
        return
    ledger.upsert_stage_run({"run_id": run_id, "stage": stage["name"], "attempt": 2, "started": int(now),
                             "status": "resumed", "log": str(logf)})
    ledger.add_event("watch", run_id, "resume", f"{stage['name']} from {checkpoint or 'start'}, driver {pid}")


def _launch_queued(project: Project, ssh: Ssh, ledger: Ledger) -> None:
    queued: dict[str, list[str]] = {}
    for r in ledger.runs(state="queued"):
        queued.setdefault(str(r["batch"]), []).append(str(r["label"]))
    for name, labels in queued.items():
        try:
            launch.launch(project, config.load_batch(project, name), ssh, ledger, only=labels[:1], stagger_s=0,
                          allow_dirty=True)
        except Exception:  # one bad batch must not end the watcher
            log.exception("queued batch %s", name)


# boards and the cycle

def _boards(project: Project, ssh: Ssh, ledger: Ledger, notifiers: list[Notifier], now: float) -> None:
    probes: dict[str, Any] = {}
    for host in project.site.hosts:
        try:
            probes[host] = asdict(ssh.probe(host))
        except HostError as e:
            probes[host] = {"error": str(e)}
    bdir = project.data / "board"
    ledger.write_board_json(bdir / "board.json", probes)
    retired = {b["batch"] for b in ledger.batches() if b.get("retired")}
    rows = [r for r in ledger.runs() if r["batch"] not in retired]
    config.save_text(bdir / "status.html", board.status_html(rows, ledger.events(n=50), probes, now))
    params = [dict(r) for r in ledger.db.execute("SELECT run_id, key, value, source FROM params")]
    plotly = board.PLOTLY_FILE if (bdir / board.PLOTLY_FILE).is_file() else board.PLOTLY_URL
    config.save_text(bdir / "compare.html", board.compare_html(rows, params, ledger.metrics(), plotly))
    text = tgfmt.board(rows, now=now, totals=metrics.step_totals(project))
    for n in notifiers:
        n.board(text)


def cycle(project: Project, ssh: Ssh, ledger: Ledger, notifiers: list[Notifier], now: float | None = None,
          dry_run: bool = False) -> dict[str, str]:
    """One cycle; returns {run_id: state}. A dry run reads, classifies and prints, and writes nothing."""
    now = time.time() if now is None else now
    progress, notes = ledger.get_kv("progress", {}), ledger.get_kv("notified", {})
    heartbeats = read_heartbeats(project)
    if not dry_run:
        ingest(ledger, heartbeats)
    states: dict[str, str] = {}
    for batch, hb in heartbeats:
        run = ledger.run(hb["run_id"]) or {**hb, "batch": batch}
        state, reasons = classify(project, ssh, ledger, run, hb, now, progress)
        states[hb["run_id"]] = state
        if dry_run:
            print(f"{hb['run_id']}: {state} {'; '.join(reasons)}".rstrip())
            continue
        ledger.upsert_run({"run_id": hb["run_id"], "state": state})
        actions(project, ssh, ledger, notifiers, run, state, reasons, now, notes)
        _collect(project, ssh, ledger, run, hb, progress)
        if state == "dead":
            _resume(project, ssh, ledger, run, hb, progress, now)
    for o in orphans(project, ssh, ledger):
        states[o["key"]] = "orphan"
        if dry_run:
            print(f"{o['key']}: orphan {o['phase']} ({o['etimes']} s)")
        else:
            actions(project, ssh, ledger, notifiers, o, "orphan", [o["phase"]], now, notes)
    if dry_run:
        return states
    _launch_queued(project, ssh, ledger)
    _boards(project, ssh, ledger, notifiers, now)
    ledger.set_kv("progress", progress)
    ledger.set_kv("notified", notes)
    n = int(config.load_json(project.state / "watch.json").get("cycle") or 0) + 1
    config.save_json(project.state / "watch.json", {"ts": now, "cycle": n, "pid": os.getpid()})
    return states


def run_forever(project: Project, ssh: Ssh, ledger: Ledger, notifiers: list[Notifier], once: bool = False) -> int:
    """A cycle every limits.heartbeat_s; the notifier threads start once. With `once`: 1 when the cycle failed."""
    for n in notifiers:
        n.start()
    failed = False
    try:
        while True:
            t0 = time.time()
            try:
                # A watcher runs for weeks; the config it read at start must not rule every cycle.
                project = config.load_project(project.root)
                ssh = Ssh(project.site)
                for n in notifiers:
                    if hasattr(n, "project"):
                        n.project = project
                cycle(project, ssh, ledger, notifiers)
            except config.ConfigError as e:  # a config edit mid-way: skip this cycle, keep the service
                log.error("config not loadable, cycle skipped: %s", e)
                failed = True
            except Exception:  # the next cycle sees a fresh state; the log keeps the traceback
                log.exception("watch cycle failed")
                failed = True
            if once:
                return int(failed)
            time.sleep(max(1.0, project.limits.heartbeat_s - (time.time() - t0)))
    finally:
        for n in notifiers:
            n.stop()


def check(project: Project, notifiers: list[Notifier] = ()) -> int:
    """1 (and an alert) when watch.json is older than three cycles, else 0."""
    w = config.load_json(project.state / "watch.json")
    age = time.time() - float(w.get("ts") or 0)
    if age <= 3 * project.limits.heartbeat_s:
        return 0
    text = f"watch.json is {int(age)} s old (pid {w.get('pid')})" if w else "no watch.json"
    print(f"edr watch: {text}")
    for n in notifiers:
        n.send("watch", "", f"watch stale\n{text}")
    return 1

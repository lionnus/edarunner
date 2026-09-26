"""The watcher: one cycle over every heartbeat. See docs/design.md section 7.

The cycle reads, classifies, acts, collects, resumes, launches and writes
the boards. It never deletes a file or a tree. Its memory between cycles
is two small JSON files under data/board/: progress.json (what each run
looked like last time) and notified.json (states, alerts, grace clocks).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import board, collect, config, launch, metrics
from .guards import Refuse
from .hosts import HostError, Ssh
from .ledger import Ledger
from .model import Project
from .notify import Notifier, alert_buttons

log = logging.getLogger(__name__)
Row = dict[str, Any]

_RUN_KEYS = ("run_id", "label", "config", "host", "root", "phase", "stage", "step", "exit", "killed_by",
             "started", "updated", "disk_free_gb", "tree_gb", "counts")
_TASK_KEYS = ("started", "ended", "exit", "signature", "log")
_BUSY = ("stage:", "group:", "retry:")
_NOTIFY = {"dead", "hung", "looping", "over_budget", "host_full", "superseded", "orphan", "incomplete", "failed",
           "killed"}


# files

def _load(path: Path) -> dict:
    """A JSON file as a dict; {} when absent, or torn after three tries."""
    for _ in range(3):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def _save(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


def _hb(project: Project, run: Row) -> dict:
    return _load(project.state / str(run.get("batch")) / f"{run['run_id']}.json")


def _keep(project: Project, run: Row) -> dict:
    return _load(project.state / str(run.get("batch")) / f"{run['run_id']}.keep.json")


def read_heartbeats(project: Project) -> list[tuple[str, dict]]:
    """Every heartbeat of every batch without RETIRED, as (batch, heartbeat)."""
    out = []
    for bdir in sorted(p for p in project.state.glob("*") if p.is_dir() and p.name != "bin"):
        if (bdir / "RETIRED").exists():
            continue
        for f in sorted(bdir.glob("*.json")):
            if not f.name.endswith((".spec.json", ".keep.json")):
                hb = _load(f)
                if hb.get("run_id"):
                    out.append((bdir.name, hb))
    return out


def ingest(ledger: Ledger, heartbeats: list[tuple[str, dict]]) -> None:
    """Upsert `runs` and `stage_runs` from the heartbeats; `state` is left to classify."""
    for batch, hb in heartbeats:
        ledger.upsert_run({"batch": batch, **{k: hb.get(k) for k in _RUN_KEYS}})
        stage = hb.get("stage") or ""
        stages = hb.get("stages") or ({stage: {"status": "running", "log": hb.get("log")}} if stage else {})
        for name, s in stages.items():
            status = s.get("status") or "running"
            # A stage still running in a finished run ended with the run: killed, stopped or failed.
            if status == "running" and not board.is_live(hb):
                status = board.state_of(hb)
            ledger.upsert_stage_run({"run_id": hb["run_id"], "stage": name, "attempt": s.get("attempt") or 1,
                                     "status": status, "started": s.get("started"), "ended": s.get("ended"),
                                     "exit": s.get("exit"), "log": s.get("log")})
        for tid, t in (hb.get("tasks") or {}).items():
            ledger.upsert_stage_run({"run_id": hb["run_id"], "stage": stage, "task": tid, "status": t.get("phase"),
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
    """The state of one run by the table of section 7, and every reason found."""
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
            handle = f"{run.get('label')}@{run.get('batch')}"
            body = f"{handle} {state}\n{text}\n{run_id or run.get('label')} on {run.get('host')}, {run.get('phase')}"
            # A repeat send edits the earlier message in place and keeps its buttons.
            ids = [n.send(state, run.get("key") or run_id, body, alert_buttons(handle) if run_id else None)
                   for n in notifiers]
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
    finished, running = collect.stage_state(project, hb)
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
    done = {t: config.resolve_task(project, t) for t, p in tasks.items() if p == "done"}
    rows = metrics.extract(project, run, project.data / "results", done)
    n = sum(ledger.add_metric(r) for r in rows if r["value"] is not None)
    if not rec.get("params"):
        ledger.set_params(run["run_id"], _params(project, run), "spec")
        rec["params"] = True
    log.info("%s: %d files, %d new metrics", run["run_id"], res.files, n)


def _resume(project: Project, ssh: Ssh, ledger: Ledger, run: Row, hb: dict, progress: dict, now: float) -> None:
    rec, run_id = progress.setdefault(run["run_id"], {}), run["run_id"]
    spec_path = project.state / str(run["batch"]) / f"{run_id}.spec.json"
    spec = _load(spec_path)
    stage = next((s for s in spec.get("stages") or [] if s.get("name") == hb.get("stage")), None)
    if rec.get("resumed") or not stage or not stage.get("resume"):
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
    _save(spec_path, spec)
    driver, logf = project.state / "bin" / str(run["batch"]) / "edr_driver.py", spec_path.with_name(f"{run_id}.driver.log")
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
    _save(bdir / "status.html", board.status_html(rows, ledger.events(n=50), probes, now))
    params = [dict(r) for r in ledger.db.execute("SELECT run_id, key, value, source FROM params")]
    plotly = board.ensure_plotly(project.data)
    _save(bdir / "compare.html", board.compare_html(rows, params, ledger.metrics(), plotly.name if plotly else board.PLOTLY_URL))
    text = board.narrow(rows, now=now)
    for n in notifiers:
        n.board(text)


def cycle(project: Project, ssh: Ssh, ledger: Ledger, notifiers: list[Notifier], now: float | None = None,
          dry_run: bool = False) -> dict[str, str]:
    """One cycle; returns {run_id: state}. A dry run reads, classifies and prints, and writes nothing."""
    now = time.time() if now is None else now
    bdir = project.data / "board"
    progress, notes = _load(bdir / "progress.json"), _load(bdir / "notified.json")
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
    _save(bdir / "progress.json", progress)
    _save(bdir / "notified.json", notes)
    n = int(_load(project.state / "watch.json").get("cycle") or 0) + 1
    _save(project.state / "watch.json", {"ts": now, "cycle": n, "pid": os.getpid()})
    return states


def run_forever(project: Project, ssh: Ssh, ledger: Ledger, notifiers: list[Notifier], once: bool = False) -> None:
    """A cycle every limits.heartbeat_s; the notifier threads start once."""
    for n in notifiers:
        n.start()
    try:
        while True:
            t0 = time.time()
            try:
                cycle(project, ssh, ledger, notifiers)
            except Exception:  # the next cycle sees a fresh state; the log keeps the traceback
                log.exception("watch cycle failed")
            if once:
                return
            time.sleep(max(1.0, project.limits.heartbeat_s - (time.time() - t0)))
    finally:
        for n in notifiers:
            n.stop()


def check(project: Project, notifiers: list[Notifier] = ()) -> int:
    """1 (and an alert) when watch.json is older than three cycles, else 0."""
    w = _load(project.state / "watch.json")
    age = time.time() - float(w.get("ts") or 0)
    if age <= 3 * project.limits.heartbeat_s:
        return 0
    text = f"edr watch: watch.json is {int(age)} s old (pid {w.get('pid')})" if w else "edr watch: no watch.json"
    print(text)
    for n in notifiers:
        n.send("watch", "", text)
    return 1

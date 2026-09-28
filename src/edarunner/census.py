"""The census: one look at every host and every live run of the user, and the work that needs it.

The process that holds `~/.edr/serve.lock` takes the census each cycle: one ssh call per host of
every registered project's site, all at once, for the probe, the clock and the user's processes,
and the live heartbeats of every registered project. From it that process alone checks for
orphans, stops the newest run of a full host and sweeps the stale seat leases, once for all
projects. `census.json` keeps the probes and the live runs for the watchers and the views, and
`store.json` the alerts and clocks of this work between cycles.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import board, config, home, hosts, launch, watch
from .db import Database
from .guards import Refuse
from .hosts import HostError, HostProbe, Proc, Ssh
from .model import SCHEDULERS, Needs, Placement, Project, Site
from .notify import Notifier, alerts

log = logging.getLogger(__name__)
Row = dict[str, Any]

RESERVE_S = 1800  # a launch writes its first heartbeat well within this
SKEW_S = 60  # a host clock this far off moves the stale, dead and lease windows
# A lease this young may belong to a driver that started after the census read the heartbeats.
LEASE_YOUNG_S = 120
_LIVE_KEYS = ("run_id", "label", "host", "phase", "stage", "started", "updated", "driver_pid", "cpu_pct", "rss_gb",
              "tree_gb", "host_full")


# the registered projects and their live runs

def projects() -> dict[str, Project]:
    """Every registered project whose files load; one that does not load is logged and left out."""
    d = home.root() / "projects"
    out: dict[str, Project] = {}
    for name in sorted(p.name for p in d.iterdir()) if d.is_dir() else []:
        if (path := home.owner(name)) is not None:
            try:
                out[name] = config.load_project(path)
            except config.ConfigError as e:
                log.warning("%s: %s", name, e)
    return out


def live_runs(projects: Iterable[Project], now: float) -> list[Row]:
    """The live runs: every heartbeat that is not terminal and younger than `dead_s` of its project."""
    out = []
    for p in projects:
        for batch, hb in watch.read_heartbeats(p):
            if board.is_live(hb) and now - float(hb.get("updated") or 0) < p.limits.dead_s:
                out.append({"project": p.project, "batch": batch, **{k: hb.get(k) for k in _LIVE_KEYS}})
    return out


def _reservation(project: str, run_id: str) -> Path:
    return home.root() / "reservations" / f"{project}.{run_id}"


def reserve(project: Project, plans: list) -> None:
    """Record the host of each plan that will start, so the next placement counts it before its first heartbeat."""
    for p in plans:
        if p.host and p.spec and not p.problems:
            config.save_json(_reservation(project.project, p.run_id), {
                "project": project.project, "run_id": p.run_id, "host": p.host, "heartbeat": p.spec["state_file"],
                "ts": time.time()})


def unreserve(project: Project, run_id: str) -> None:
    """Drop the reservation of a run that did not start."""
    _reservation(project.project, run_id).unlink(missing_ok=True)


def reservations(now: float, drop: bool = False) -> list[Row]:
    """The placements in flight: the reservations younger than RESERVE_S whose run wrote no heartbeat yet.

    With `drop` the others are removed."""
    d, out = home.root() / "reservations", []
    for f in sorted(d.iterdir()) if d.is_dir() else []:
        r = config.load_json(f)
        if now - float(r.get("ts") or 0) < RESERVE_S and r.get("host") and not Path(str(r.get("heartbeat"))).exists():
            out.append(r)
        elif drop:
            f.unlink(missing_ok=True)
    return out


def running_per_host(project: Project, now: float) -> Counter:
    """Your runs per host: the live runs of every registered project and of `project`, plus the placements in flight."""
    return Counter(r["host"] for r in live_runs({**projects(), project.project: project}.values(), now) + reservations(now))


# the census

def host_sites(projects: dict[str, Project]) -> dict[str, Site]:
    """The site of each host that a registered project reaches over ssh; the first project that names a host wins."""
    out: dict[str, Site] = {}
    for p in projects.values():
        if p.site.scheduler.backend not in SCHEDULERS:
            for h in p.site.hosts:
                out.setdefault(h, p.site)
    return out


def take(projects: dict[str, Project], now: float) -> Row:
    """Probe every ssh host of the sites of `projects` at once, with its clock and our processes, and add the live runs."""
    sites = host_sites(projects)

    def one(host: str) -> tuple[dict, list[Proc]]:
        try:
            probe, procs = Ssh(sites[host]).census(host)
            return asdict(probe), procs
        except HostError as e:
            return {"error": str(e)}, []

    names = list(sites)
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        found = dict(zip(names, pool.map(one, names)))
    return {"ts": now, "hosts": {h: probe for h, (probe, _) in found.items()},
            "procs": {h: procs for h, (_, procs) in found.items()}, "runs": live_runs(projects.values(), now)}


def read(max_age: float, now: float | None = None) -> Row:
    """The last census from `census.json`, or {} when it is older than `max_age` seconds."""
    c = config.load_json(home.root() / "census.json")
    return c if (now or time.time()) - float(c.get("ts") or 0) <= max_age else {}


def probes(project: Project, max_age: float) -> dict[str, dict] | None:
    """The probes of the project's hosts from a census younger than `max_age`, or None when one is missing."""
    found = read(max_age).get("hosts") or {}
    return None if set(project.site.hosts) - set(found) else {h: found[h] for h in project.site.hosts}


# the work of the user

def _event(project: Project, run_id: str, kind: str, text: str) -> None:
    """One event in the database of `project`; a database that does not open is logged."""
    try:
        with Database(project.data / "edr.db") as db:
            db.add_event("watch", run_id, kind, text)
    except (sqlite3.Error, OSError) as e:
        log.warning("%s: event %s %s not recorded: %s", project.project, kind, text, e)


def _state(p: Project, hb: dict, pids: set[int], now: float) -> str:
    """How a run of a process ended: the phase class, `dead` for a gone driver, or "" while it runs."""
    if not board.is_live(hb):
        return board.state_of(hb)
    gone = now - float(hb.get("updated") or 0) > p.limits.dead_s and hb.get("driver_pid") not in pids
    return "dead" if gone else ""


def orphans(census: Row, projects: dict[str, Project], now: float) -> list[Row]:
    """Our tool processes that no live run of a registered project owns, each with the project it belongs to.

    A process with `EDR_RUN_ID` belongs to that run, of whichever project has it. A live run owns it; a
    run whose heartbeat ended, or whose driver is gone after `dead_s`, leaves it an orphan. A run id
    that no project knows belongs to an unregistered project and is left alone, unless the cwd or
    the command line of the process lies in the tree `/<project>/<run_id>` of a registered project
    under its safety marker. A process without `EDR_RUN_ID` is owned when its cwd or command line
    holds the safety marker of a registered project."""
    runs: dict[str, list[tuple[Project, dict]]] = {}
    for p in projects.values():
        for batch, hb in watch.read_heartbeats(p):
            runs.setdefault(hb["run_id"], []).append((p, {**hb, "batch": batch}))
    sites = host_sites(projects)
    out = []
    for host, procs in (census.get("procs") or {}).items():
        site = sites.get(host)
        if site is None or not site.tool_procs:
            continue
        rx, pids = re.compile(site.tool_procs), {p.pid for p in procs}
        for proc in procs:
            if not rx.search(proc.comm):
                continue
            texts, owner, run, ended = (proc.cwd, proc.args), None, None, ""
            if proc.run_id in runs:
                states = [(p, hb, _state(p, hb, pids if hb.get("host") == host else set(), now))
                          for p, hb in runs[proc.run_id]]
                if any(not s for _, _, s in states):
                    continue
                owner, run, ended = states[0]
            elif proc.run_id:
                owner = next((p for p in projects.values() if any(
                    p.safety.marker in t and f"/{p.project}/{proc.run_id}" in t for t in texts)), None)
                if owner is None:
                    continue
                ended = "unknown"
            elif any(p.safety.marker in t for p in projects.values() for t in texts):
                continue
            out.append({"key": f"orphan:{host}:{proc.pid}", "host": host, "pid": proc.pid, "label": proc.comm,
                        "phase": proc.args, "etimes": proc.etimes, "cwd": proc.cwd, "owner": proc.run_id,
                        "owner_handle": f"{owner.project}/{board.handle(run)}" if run and owner else "", "owner_state": ended,
                        "project": owner.project if owner else "", "run_id": run["run_id"] if run else ""})
    return out


def _alert(store: Row, notifiers: list[Notifier], key: str, text: str, make: Any, now: float) -> Row:
    """The note of alert `key`; the alert goes out when its text is new."""
    rec = store.setdefault("alerts", {}).setdefault(key, {"since": now})
    rec["seen"] = now
    if rec.get("text") != text:
        a = make()
        for n in notifiers:
            n.send(a)
        rec["text"] = text
    return rec


def _orphans(census: Row, projects: dict[str, Project], notifiers: list[Notifier], store: Row, now: float) -> None:
    for o in orphans(census, projects, now):
        p = projects.get(o["project"])
        rec = _alert(store, notifiers, o["key"], o["phase"], lambda o=o, p=p: alerts.orphan_alert(p, o), now)
        if rec.get("since") == now and p is not None:
            _event(p, o["run_id"], "orphan", f"{o['label']} pid {o['pid']} on {o['host']}: {o['phase']}")
        if p is None or not p.limits.kill_orphan or rec.get("killed") or now - rec["since"] < p.limits.grace_s:
            continue
        rec["killed"] = True
        rc, _, err = Ssh(p.site).run(o["host"], f"kill -TERM {int(o['pid'])}")
        _event(p, o["run_id"], "kill", f"orphan: TERM pid {o['pid']} on {o['host']}" + (f": {err.strip()}" if rc else ""))


def _full_hosts(census: Row, projects: dict[str, Project], store: Row, now: float) -> None:
    """Stop the newest run of each full host once per `grace_s` of its project, whatever project it belongs to."""
    clocks = store.setdefault("full", {})
    full = {r["host"] for r in census["runs"] if r.get("host_full")}
    for host in set(clocks) - full:
        del clocks[host]
    for host in sorted(full):
        since = clocks.setdefault(host, now)
        target = max((r for r in census["runs"] if r["host"] == host and r["project"] in projects),
                     key=lambda r: float(r.get("started") or 0), default=None)
        if target is None:
            continue
        p = projects[target["project"]]
        hb = config.load_json(p.state_dir / target["batch"] / f"{target['run_id']}.json")
        keep = config.load_json(p.state_dir / target["batch"] / f"{target['run_id']}.keep.json")
        if now - since < p.limits.grace_s or keep.get("ack"):
            continue
        clocks[host] = now
        why = f"{host} full: below {hosts.floor(p.site, host):g} GB free"
        try:
            with Database(p.data / "edr.db") as db:
                launch.stop(Ssh(p.site), db, {**target, **(db.run(target["run_id"]) or {})}, hb, now=True, why=why,
                            actor="watch")
        except (HostError, Refuse, OSError, sqlite3.Error) as e:
            _event(p, target["run_id"], "kill", f"host_full: failed: {e}")


def sweep_leases(projects: dict[str, Project], now: float) -> list[str]:
    """Remove each stale seat lease with an event in its project, and return their paths.

    A lease is stale when its run has no heartbeat in a registered project, has ended, has a heartbeat
    older than `dead_s`, has left the lease's stage, or when the lease is older than its stage budget."""
    heartbeats = {(p.project, hb["run_id"]): (p, hb) for p in projects.values() for _, hb in watch.read_heartbeats(p)}
    out = []
    for f in sorted((home.root() / "leases").glob("*/*")):
        lease = config.load_json(f)
        age = now - float(lease.get("ts") or 0)
        if f.name.startswith(".") or age < LEASE_YOUNG_S:
            continue
        p, hb = heartbeats.get((lease.get("project"), lease.get("run_id")), (projects.get(str(lease.get("project"))), None))
        if hb is None:
            why = "no heartbeat of the run in a registered project"
        elif not board.is_live(hb):
            why = f"the run ended {hb.get('phase')}"
        elif now - float(hb.get("updated") or 0) > p.limits.dead_s:
            why = "the run is dead"
        elif hb.get("stage") != lease.get("stage"):
            why = f"the run left stage {lease.get('stage')}"
        elif lease.get("budget_s") and age > float(lease["budget_s"]):
            why = f"older than the stage budget of {int(lease['budget_s'])} s"
        else:
            continue
        f.unlink(missing_ok=True)
        out.append(str(f))
        text = f"stale lease {f.parent.name}/{f.name}: {why}"
        if p is not None:
            _event(p, str(lease.get("run_id") or ""), "lease", text)
        else:
            log.info("%s", text)
    return out


def _clocks(census: Row, notifiers: list[Notifier], store: Row, now: float) -> None:
    for host, probe in census["hosts"].items():
        skew = probe.get("skew_s") or 0
        if abs(skew) > SKEW_S:
            _alert(store, notifiers, f"clock:{host}", f"{skew:+.0f}", lambda h=host, s=skew: alerts.clock_alert(h, s), now)


def work(notifiers: list[Notifier], now: float | None = None, own: Project | None = None) -> Row:
    """Take the census and do the work of the user; `own` is the project of a watcher that holds serve.lock."""
    now = time.time() if now is None else now
    found = projects()
    if own is not None:
        found.setdefault(own.project, own)
    census = take(found, now)
    config.save_json(home.root() / "census.json", {k: census[k] for k in ("ts", "hosts", "runs")})
    store = config.load_json(home.root() / "store.json")
    for step in (lambda: _orphans(census, found, notifiers, store, now), lambda: _full_hosts(census, found, store, now),
                 lambda: sweep_leases(found, now), lambda: _clocks(census, notifiers, store, now)):
        try:
            step()
        except Exception:  # one step that fails must not stop the others
            log.exception("census work")
    # An alert that no longer holds is forgotten, so it comes again when it returns.
    store["alerts"] = {k: v for k, v in (store.get("alerts") or {}).items() if v.get("seen") == now}
    reservations(now, drop=True)
    config.save_json(home.root() / "store.json", store)
    return census


# the view of the hosts

def host_view(probes: dict[str, HostProbe | str], live: list[Row], floors: dict[str, float], pl: Placement) -> list[Row]:
    """One row per host: its free room, the live runs of yours on it by project with the cores, RAM and scratch
    they use, whether a run can start there under `pl` and above its floor, and why not, and a note when your
    runs fill it.

    The hosts where a run can start come first, the most free cores first, then the full ones, then
    those that did not answer."""
    out = []
    for host, p in probes.items():
        mine = [r for r in live if r.get("host") == host]
        row: Row = {"host": host, "runs": dict(Counter(r["project"] for r in mine)),
                    "our_cores": round(sum(float(r.get("cpu_pct") or 0) for r in mine) / 100, 1),
                    "our_ram_gb": round(sum(float(r.get("rss_gb") or 0) for r in mine), 1),
                    "our_gb": round(sum(float(r.get("tree_gb") or 0) for r in mine), 1)}
        if isinstance(p, str):
            out.append({**row, "error": p, "start": None, "why": "no answer", "note": ""})
            continue
        keep = floors[host]
        why = hosts.why_not(pl, p, len(mine), Needs(), keep)
        note = []
        if p.free_gb < keep <= p.free_gb + row["our_gb"]:
            note.append(f"your trees hold {row['our_gb']:g} GB and push its scratch under the floor of {keep:g} GB")
        busy = p.cores - p.free_cores
        if p.free_cores < pl.min_free_cores and busy and row["our_cores"] >= busy / 2:
            note.append(f"your runs use {row['our_cores']:g} of its {busy:g} busy cores")
        used = p.total_ram_gb - p.free_ram_gb
        if p.free_ram_gb < pl.min_free_ram_gb and used and row["our_ram_gb"] >= used / 2:
            note.append(f"your runs hold {row['our_ram_gb']:g} of its {used:g} GB of RAM in use")
        if abs(p.skew_s) > SKEW_S:
            note.append(f"its clock is {p.skew_s:+.0f} s off the head node")
        out.append({**asdict(p), **row, "floor_gb": keep, "start": not why, "why": why, "note": "; ".join(note)})
    return sorted(out, key=lambda r: ({True: 0, False: 1, None: 2}[r["start"]], -float(r.get("free_cores") or 0), r["host"]))

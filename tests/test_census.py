"""census.py: the live runs of every registered project, the census, and the work of the user on two demo projects."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from helpers_watch import Rec

from edarunner import census, config, home, launch
from edarunner.db import Database
from edarunner.hosts import HostProbe, Proc, Ssh
from edarunner.model import Placement
from helpers_driver import DEMO

NOW = 1_800_000_000.0
DATE = "20260926_1200"


def heartbeat(project, label: str, batch: str = "demo", age: float = 5, now: float = NOW, **extra) -> dict:
    run_id = f"{DATE}_{label}_demo_gabc1234"
    hb = {"run_id": run_id, "label": label, "batch": batch, "host": "local", "phase": "stage:synth", "stage": "synth",
          "started": now - 3600, "updated": now - age, "driver_pid": 4242, "pgids": [4300], "cpu_pct": 150.0,
          "rss_gb": 2.0, "tree_gb": 10.0, **extra}
    (project.state_dir / batch).mkdir(parents=True, exist_ok=True)
    (project.state_dir / batch / f"{run_id}.json").write_text(json.dumps(hb))
    return hb


class Hosts:
    """What the fake ssh of the census answers: a probe and our processes per host, and every kill it was asked for."""

    def __init__(self) -> None:
        self.probes = {"local": HostProbe("local", 3.0, 6.0, "/tmp", 50.0, cores=4, load=1.0, total_ram_gb=8.0,
                                          total_gb=100.0)}
        self.procs: list[Proc] = []
        self.killed: list[str] = []


@pytest.fixture
def fake(monkeypatch) -> Hosts:
    h = Hosts()

    class FakeSsh(Ssh):
        def census(self, host):
            return h.probes[host], list(h.procs)

        def run(self, host, cmd, timeout_s=None):
            h.killed.append(cmd)
            return 0, "", ""

    monkeypatch.setattr(census, "Ssh", FakeSsh)
    return h


def test_the_live_runs_of_every_project_count_on_their_host(two, user_root: Path) -> None:
    alpha, beta, now = two["alpha"], two["beta"], time.time()
    heartbeat(alpha, "a", now=now)
    heartbeat(alpha, "gone", phase="done", now=now)
    heartbeat(alpha, "old", age=alpha.limits.dead_s + 1, now=now)
    heartbeat(beta, "b", host="hostA", now=now)
    live = census.live_runs(census.projects().values(), now)
    assert {(r["project"], r["label"], r["host"]) for r in live} == {("alpha", "a", "local"), ("beta", "b", "hostA")}
    plan = launch.RunPlan(run_id="r1", label="x", host="hostA", root="/r", spec={"state_file": str(user_root / "r1.json")},
                          queued=False)
    census.reserve(beta, [plan, launch.RunPlan(run_id="r2", label="y", host=None, root="", spec={}, queued=True)])
    assert census.running_per_host(alpha, now) == {"local": 1, "hostA": 2}
    (user_root / "r1.json").write_text("{}")  # the first heartbeat of the reserved run
    assert census.running_per_host(alpha, now) == {"local": 1, "hostA": 1}
    (user_root / "r1.json").unlink()
    assert census.reservations(now + census.RESERVE_S + 1) == [] and census.reservations(now)
    census.unreserve(beta, "r1")
    assert census.reservations(now) == []


def test_the_census_writes_probes_and_live_runs_for_the_watchers(two, fake, user_root: Path) -> None:
    heartbeat(two["alpha"], "a")
    fake.probes["local"].skew_s = 75.0
    rec = Rec()
    census.work([rec], NOW)
    written = json.loads((user_root / "census.json").read_text())
    assert written["ts"] == NOW and written["hosts"]["local"]["free_cores"] == 3.0
    assert [(r["project"], r["label"]) for r in written["runs"]] == [("alpha", "a")]
    assert census.read(60, NOW + 30)["ts"] == NOW and census.read(60, NOW + 61) == {}
    assert rec.sent == [("clock", "local")] and "75 s ahead of the head node" in rec.alerts["local"].about
    census.work([rec], NOW + 1)
    assert len(rec.sent) == 1  # one alert per host while the skew holds


def orphan_procs(alpha, beta) -> list[Proc]:
    live, dead, done = (heartbeat(alpha, "a")["run_id"], heartbeat(beta, "d", age=beta.limits.dead_s + 1)["run_id"],
                        heartbeat(beta, "g", phase="done")["run_id"])
    return [Proc(501, 60, 99.0, "sleep", "sleep 1", "/home/me", live),
            Proc(502, 60, 99.0, "sleep", "sleep 2", "/home/me", dead),
            Proc(503, 60, 99.0, "sleep", "sleep 3", "/home/me", done),
            Proc(504, 60, 99.0, "sleep", "sleep 4", "/scratch/x/edr/other/r9/pnr", "r9"),
            Proc(505, 60, 99.0, "sleep", "sleep 5", "/scratch/x/edr/beta/r8/pnr", "r8"),
            Proc(506, 60, 99.0, "sleep", "sleep -f /scratch/x/edr/other/r1/main.tcl", "/home/me"),
            Proc(507, 60, 99.0, "sleep", "sleep 7", "/home/me"),
            Proc(508, 60, 0.0, "bash", "bash", "/home/me")]


def test_one_alert_per_orphan_over_every_project(two, fake) -> None:
    fake.procs = orphan_procs(two["alpha"], two["beta"])
    rec = Rec()
    census.work([rec], NOW)
    found = {k.rsplit(":", 1)[1]: a.about.removesuffix(" It may hold a licence seat.")
             for k, a in rec.alerts.items() if a.kind == "orphan"}
    you = "Your process sleep runs on local"
    assert found == {"502": f"{you} for the run beta/d@demo, whose driver is gone.",
                     "503": f"{you} for the run beta/g@demo, which has ended (done).",
                     "505": f"{you} in the tree of the run r8 of beta, but that project has no record of the run.",
                     "507": f"{you}, and no edarunner run owns it."}
    census.work([rec], NOW + 1)
    assert len(rec.sent) == 4 and fake.killed == []
    with Database(two["beta"].data / "edr.db") as db:
        assert sorted(e["kind"] for e in db.events()) == ["orphan", "orphan", "orphan"]


def test_an_orphan_is_killed_only_with_kill_orphan_of_its_project(two, fake, monkeypatch) -> None:
    fake.procs = orphan_procs(two["alpha"], two["beta"])
    real = census.projects

    def projects():
        found = real()
        found["beta"].limits.kill_orphan, found["beta"].limits.grace_s = True, 10
        return found

    monkeypatch.setattr(census, "projects", projects)
    census.work([Rec()], NOW)
    assert fake.killed == []
    census.work([Rec()], NOW + 11)
    assert sorted(fake.killed) == ["kill -TERM 502", "kill -TERM 503", "kill -TERM 505"]  # never 507, of no project
    census.work([Rec()], NOW + 12)
    assert len(fake.killed) == 3


def test_a_full_host_loses_one_run_once_per_grace(two, fake, monkeypatch) -> None:
    alpha, beta = two["alpha"], two["beta"]
    heartbeat(alpha, "a", host_full=True, started=NOW - 7200)
    heartbeat(beta, "b", started=NOW - 600)
    stops: list[tuple] = []
    monkeypatch.setattr(census.launch, "stop", lambda ssh, db, run, hb, **kw: stops.append((run["project"], run["run_id"], kw)))
    census.work([Rec()], NOW)
    assert stops == []
    keep = beta.state_dir / "demo" / f"{DATE}_b_demo_gabc1234.keep.json"
    keep.write_text('{"hours": 24}')
    os.utime(keep, (NOW, NOW))  # a keep does not hold off this stop: the full disk blocks every other user
    census.work([Rec()], NOW + beta.limits.grace_s - 1)
    assert stops == []
    census.work([Rec()], NOW + beta.limits.grace_s + 1)
    assert [(p, r) for p, r, _ in stops] == [("beta", f"{DATE}_b_demo_gabc1234")] and stops[0][2]["now"]
    census.work([Rec()], NOW + beta.limits.grace_s + 2)
    assert len(stops) == 1
    heartbeat(alpha, "a", host_full=False)
    census.work([Rec()], NOW + beta.limits.grace_s + 3)
    assert json.loads((home.root() / "store.json").read_text())["full"] == {}


def test_the_sweep_removes_the_stale_leases_of_every_project(two, fake, user_root: Path) -> None:
    alpha, beta = two["alpha"], two["beta"]
    heartbeat(alpha, "live")
    heartbeat(beta, "moved", stage="pnr")
    heartbeat(beta, "ended", phase="KILLED:SIGKILL")
    d = user_root / "leases" / "demo"
    d.mkdir(parents=True)

    def lease(project: str, label: str, age: float) -> str:
        run_id = f"{DATE}_{label}_demo_gabc1234"
        name = f"{project}.{run_id}.synth.0"
        (d / name).write_text(json.dumps({"project": project, "run_id": run_id, "stage": "synth", "ts": NOW - age}))
        return name

    keep = [lease("alpha", "live", 300), lease("beta", "young", 10)]
    lease("beta", "moved", 300)
    lease("beta", "ended", 300)
    lease("gamma", "other", 300)
    census.work([Rec()], NOW)
    assert sorted(p.name for p in d.iterdir()) == sorted(keep)
    with Database(beta.data / "edr.db") as db:
        texts = sorted(e["text"] for e in db.events() if e["kind"] == "lease")
    assert texts == [f"stale lease demo/beta.{DATE}_ended_demo_gabc1234.synth.0: the run ended KILLED:SIGKILL",
                     f"stale lease demo/beta.{DATE}_moved_demo_gabc1234.synth.0: the run left stage synth"]


def test_the_host_view_puts_the_hosts_where_a_run_can_start_first(two) -> None:
    probes = {"full": HostProbe("full", 30.0, 100.0, "/s", 40.0, cores=64, load=34.0, total_ram_gb=256.0, total_gb=1000.0),
              "busy": HostProbe("busy", 2.0, 100.0, "/s", 900.0, cores=64, load=62.0, total_ram_gb=256.0, total_gb=1000.0),
              "free": HostProbe("free", 60.0, 200.0, "/s", 900.0, cores=64, load=4.0, total_ram_gb=256.0, total_gb=1000.0),
              "silent": "silent: rc 255: timeout"}
    live = [{"project": "alpha", "host": "full", "cpu_pct": 400.0, "rss_gb": 8.0, "tree_gb": 700.0},
            {"project": "beta", "host": "busy", "cpu_pct": 4000.0, "rss_gb": 20.0, "tree_gb": 5.0},
            {"project": "beta", "host": "busy", "cpu_pct": 2000.0, "rss_gb": 20.0, "tree_gb": 5.0}]
    rows = census.host_view(probes, live, dict.fromkeys(probes, 100), Placement(max_per_host=3, min_free_cores=8,
                                                                                    min_free_ram_gb=16))
    assert [(r["host"], r["start"]) for r in rows] == [("free", True), ("full", False), ("busy", False), ("silent", None)]
    full, busy = rows[1], rows[2]
    assert full["why"] == "40 GB scratch free, under the floor of 100 GB" and full["runs"] == {"alpha": 1}
    assert full["note"] == "your trees hold 700 GB and push its scratch under the floor of 100 GB"
    assert busy["why"] == "2 cores free, a run needs 8" and busy["note"] == "your runs use 60 of its 62 busy cores"
    assert (busy["our_cores"], busy["our_ram_gb"], busy["our_gb"]) == (60.0, 40.0, 10.0)
    assert rows[0]["why"] == rows[0]["note"] == "" and rows[3]["error"] == "silent: rc 255: timeout"


def test_placement_counts_your_runs_of_every_project_and_the_launches_in_flight(two, user_root: Path) -> None:
    alpha, beta, now = two["alpha"], two["beta"], time.time()
    alpha.placement.max_per_host = 2
    batch = config.load_batch(alpha, "demo")
    for job in batch.jobs:
        job.host = "auto"
    probes = {"local": HostProbe("local", 64.0, 256.0, "/tmp", 900.0, cores=64, total_ram_gb=256.0, total_gb=1000.0)}
    with Database(alpha.data / "edr.db") as db:
        first = launch.plan(alpha, batch, Ssh(alpha.site), db, probes=probes, reserve=True)
        assert [p.host for p in first] == ["local", "local"]
        assert sorted(r["run_id"] for r in census.reservations(now)) == sorted(p.run_id for p in first)
        heartbeat(beta, "b", now=now)  # beta's run on local counts against alpha's max_per_host
        assert census.running_per_host(alpha, now) == {"local": 3}
        again = launch.plan(alpha, batch, Ssh(alpha.site), db, probes=probes)
        assert [(p.host, p.queued) for p in again] == [(None, True), (None, True)]
    assert (user_root / "place.lock").exists()

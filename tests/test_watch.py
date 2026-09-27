"""Tests of edarunner.watch: fake heartbeats in a tmp state dir, host local, a fake ssh, a recording notifier."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from edarunner import board, collect, config, launch, watch
from edarunner.config import load_project
from edarunner.hosts import HostProbe, Ssh
from edarunner.ledger import Ledger
from edarunner.notify import Notifier

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
NOW = 1_800_000_000.0
OLD, NEW = "20260926_1200", "20260926_1300"


def rid(label: str, date: str = OLD, src: str = "gabc1234") -> str:
    return f"{date}_{label}_demo_{src}"


class FakeSsh(Ssh):
    """Real local commands; canned probe, pid check, tool processes and kills."""

    def __init__(self, site) -> None:
        super().__init__(site)
        self.alive: dict[int, bool] = {}
        self.procs: list[tuple] = []
        self.killed: list[tuple] = []

    def probe(self, host):
        return HostProbe(host, 4.0, 8.0, "/tmp/x", 50.0)

    def pid_alive(self, host, pid):
        return self.alive.get(pid, False)

    def tool_processes(self, host, pattern):
        return list(self.procs)

    def kill_pgid(self, host, pgid, sig="TERM"):
        self.killed.append(("pgid", pgid, sig))
        return True

    def run(self, host, cmd, timeout_s=None):
        if isinstance(cmd, str) and cmd.startswith("kill -0 "):
            return (0 if self.alive.get(int(cmd.rsplit("-", 1)[1])) else 1), "", ""
        if isinstance(cmd, str) and cmd.startswith("kill "):
            self.killed.append(("cmd", cmd))
            return 0, "", ""
        return super().run(host, cmd, timeout_s)


class Rec(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.boards: list[str] = []
        self.started = 0

    def start(self):
        self.started += 1

    def send(self, kind, run_id, text, buttons=None):
        self.sent.append((kind, run_id))
        return str(len(self.sent))

    def board(self, text):
        self.boards.append(text)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.project = replace(load_project(DEMO), state=tmp_path / "state", data=tmp_path / "data")
        lim = self.project.limits
        lim.stale_s, lim.dead_s, lim.hung_s, lim.grace_s = 30, 90, 3600, 10
        self.ssh = FakeSsh(self.project.site)
        self.ledger = Ledger(tmp_path / "data" / "edr.db")
        self.notifier = Rec()

    def heartbeat(self, label: str, batch: str = "demo", phase: str = "stage:synth", age: float = 5,
                  date: str = OLD, src: str = "gabc1234", **extra) -> dict:
        run_id = rid(label, date, src)
        root = self.tmp / "scratch" / "edr" / "demo" / run_id
        (root / "log").mkdir(parents=True, exist_ok=True)
        (root / "log" / "synth.log").write_text("step 1\n")
        hb = {"schema": 1, "run_id": run_id, "batch": batch, "label": label, "config": "demo", "host": "local",
              "root": str(root), "driver_pid": 4242, "pgids": [4300], "phase": phase, "stage": "synth", "step": 1,
              "step_name": "analyze", "tasks": {}, "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 0,
              "queued": 0}, "started": NOW - 3600, "updated": NOW - age, "elapsed_s": 3600 - age,
              "disk_free_gb": 100.0, "tree_gb": 1.0, "exit": None, "killed_by": None, "last_cmd": "x",
              "last_log": "step 1", "log": str(root / "log" / "synth.log")}
        hb.update(extra)
        (self.project.state / batch).mkdir(parents=True, exist_ok=True)
        (self.project.state / batch / f"{run_id}.json").write_text(json.dumps(hb))
        # launch writes src; the heartbeat has no src.
        self.ledger.upsert_run({"run_id": run_id, "batch": batch, "label": label, "src": src})
        return hb

    def cycle(self, now: float = NOW, **kw) -> dict[str, str]:
        return watch.cycle(self.project, self.ssh, self.ledger, [self.notifier], now=now, **kw)

    def events(self) -> list[tuple[str, str]]:
        return [(e["run_id"], e["kind"]) for e in self.ledger.events(n=500) if e["actor"] == "watch"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(board, "ensure_plotly", lambda d: None)
    e = Env(tmp_path)
    yield e
    e.ledger.close()


def listing(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def test_read_heartbeats_skips_retired_spec_keep_and_torn(env: Env) -> None:
    a = env.heartbeat("a")
    env.heartbeat("b", batch="old")
    (env.project.state / "old" / "RETIRED").touch()
    d = env.project.state / "demo"
    (d / f"{a['run_id']}.spec.json").write_text("{}")
    (d / f"{a['run_id']}.keep.json").write_text('{"hours": 1}')
    (d / f"{rid('torn')}.json").write_text('{"run_id": "torn", "phase')
    assert watch.read_heartbeats(env.project) == [("demo", a)]


def test_cycle_classifies_events_and_alerts(env: Env) -> None:
    a = env.heartbeat("a")
    env.heartbeat("a", batch="demo2", date=NEW, src="gdef5678")
    env.heartbeat("r")
    env.heartbeat("s", age=60)
    env.heartbeat("d", age=200)
    env.heartbeat("l", looping=True)
    env.heartbeat("o", over_budget="synth")
    env.heartbeat("f", host_full=True)
    env.heartbeat("g", phase="done", exit=0)
    roots = listing(env.tmp / "scratch")
    states = env.cycle()
    assert states == {rid("a"): "superseded", rid("a", NEW, "gdef5678"): "running", rid("r"): "running",
                      rid("s"): "stale", rid("d"): "dead", rid("l"): "looping", rid("o"): "over_budget",
                      rid("f"): "host_full", rid("g"): "done"}
    assert {r: env.ledger.run(r)["state"] for r in states} == states
    assert env.ledger.run(rid("a"))["counts"] == a["counts"] and env.ledger.run(rid("g"))["exit"] == 0
    assert sorted(env.events()) == sorted([(rid("a"), "superseded"), (rid("s"), "stale"), (rid("d"), "dead"),
                                           (rid("l"), "looping"), (rid("o"), "over_budget"), (rid("f"), "host_full"),
                                           (rid("g"), "done"), (rid("g"), "collect")])
    assert sorted(env.notifier.sent) == sorted([("superseded", rid("a")), ("dead", rid("d")), ("looping", rid("l")),
                                                ("over_budget", rid("o")), ("host_full", rid("f"))])
    assert len(env.notifier.boards) == 1 and "DEAD" in env.notifier.boards[0]
    bdir = env.project.data / "board"
    assert {p.name for p in bdir.iterdir()} == {"board.json", "status.html", "compare.html", "progress.json",
                                                 "notified.json"}
    assert len(json.loads((bdir / "board.json").read_text())["runs"]) == 9
    assert json.loads((env.project.state / "watch.json").read_text())["cycle"] == 1
    n_events, n_sent = len(env.events()), len(env.notifier.sent)
    assert env.cycle(NOW + 1) == states
    assert (len(env.events()), len(env.notifier.sent)) == (n_events, n_sent)
    assert json.loads((env.project.state / "watch.json").read_text())["cycle"] == 2
    assert listing(env.tmp / "scratch") == roots
    assert env.ssh.killed == [] and not list(env.project.state.glob("*/*.stop"))


def test_hung_after_unchanged_progress_and_kill(env: Env) -> None:
    lim = env.project.limits
    lim.hung_s, lim.grace_s = 0, 0
    env.heartbeat("a")
    b = env.heartbeat("b", pgids=[4400])
    (env.project.state / "demo" / f"{b['run_id']}.keep.json").write_text('{"hours": 0, "ack": true}')
    assert env.cycle() == {rid("a"): "running", rid("b"): "running"}
    lim.kill_hung = True
    assert env.cycle(NOW + 1) == {rid("a"): "hung", rid("b"): "hung"}
    assert env.ssh.killed == [("pgid", 4300, "TERM")]
    assert sorted(env.events()) == sorted([(rid("a"), "hung"), (rid("a"), "kill"), (rid("b"), "hung")])
    env.cycle(NOW + 2)
    assert len(env.ssh.killed) == 1 and env.notifier.sent == [("hung", rid("a")), ("hung", rid("b"))]


def test_host_full_stops_the_newest_after_grace(env: Env, monkeypatch) -> None:
    env.project.limits.grace_s = 5
    env.heartbeat("a", host_full=True)
    b = env.heartbeat("b", date=NEW)
    stops: list[dict] = []
    monkeypatch.setattr(launch, "stop", lambda ssh, ledger, run, hb, **kw: stops.append({**kw, "run_id": run["run_id"]}) or True)
    keep = env.project.state / "demo" / f"{b['run_id']}.keep.json"
    assert env.cycle()[rid("a")] == "host_full" and stops == []
    keep.write_text('{"hours": 1, "ack": true}')
    env.cycle(NOW + 6)
    assert stops == []
    keep.write_text('{"hours": 1}')
    env.cycle(NOW + 7)
    assert len(stops) == 1 and stops[0]["run_id"] == rid("b", NEW) and stops[0]["now"] and stops[0]["actor"] == "watch"
    env.cycle(NOW + 8)
    assert len(stops) == 1


def test_superseded_stops_after_task_unless_kept(env: Env) -> None:
    env.project.limits.grace_s = 0
    a, k, f = env.heartbeat("a"), env.heartbeat("k"), env.heartbeat("f", host_full=True)
    e = env.heartbeat("e")
    for label in ("a", "k", "f"):
        env.heartbeat(label, batch="demo2", date=NEW, src="gdef5678")
    env.heartbeat("e", batch="demo2", date=NEW, src="gdef5678", phase="FAILED:synth", exit=5)
    (env.project.state / "demo" / f"{k['run_id']}.keep.json").write_text('{"hours": 12}')
    states = env.cycle()
    assert states[rid("a")] == "superseded" and states[rid("k")] == "superseded"
    assert states[rid("f")] == "host_full" and states[rid("e")] == "running"
    stops = {p.name for p in (env.project.state / "demo").glob("*.stop")}
    assert stops == {f"{a['run_id']}.stop", f"{f['run_id']}.stop"}
    assert (env.project.state / "demo" / f"{a['run_id']}.stop").read_text().strip() == "after-task"
    assert (rid("a"), "stop") in env.events() and (rid("k"), "stop") not in env.events()
    n = env.events().count((rid("f"), "stop"))
    env.cycle(NOW + 1)
    assert env.events().count((rid("f"), "stop")) == n == 1


def test_collect_extract_and_params_once(env: Env, monkeypatch) -> None:
    hb = env.heartbeat("b_nodw", phase="done", exit=0, stage="synth", step=4, step_name="synth")
    root = Path(hb["root"])
    for n in range(4):
        (root / "reports" / str(n)).mkdir(parents=True)
        (root / "reports" / str(n) / "area.rpt").write_text(f"i_top {1000 + n * 10.5}\n")
        (root / "reports" / str(n) / "qor.rpt").write_text(f"Critical Path Slack: -0.0{n}\n")
    calls: list[str] = []
    real = collect.collect_run
    monkeypatch.setattr(collect, "collect_run", lambda *a, **k: calls.append(a[3]["run_id"]) or real(*a, **k))
    env.cycle()
    run_id = hb["run_id"]
    results = env.project.data / "results" / run_id
    assert (results / "reports" / "3" / "area.rpt").is_file() and (results / "log" / "synth.log").is_file()
    rows = env.ledger.metrics(run_ids=[run_id])
    by = {(r["stage"], r["step"], r["name"]): r["value"] for r in rows}
    assert by[("synth", 3, "area_cell_um2")] == 1031.5 and by[("synth", 0, "wns_ns")] == 0.0
    assert len(by) == 8 and all(v is not None for v in by.values())
    params = {r["key"]: r["value"] for r in env.ledger.db.execute("SELECT key, value FROM params WHERE run_id=?", (run_id,))}
    assert params == {"config": "demo", "DW": "0", "src": "gabc1234"}
    assert (run_id, "collect") not in env.events()
    env.cycle(NOW + 1)
    assert calls == [run_id] and len(env.ledger.metrics(run_ids=[run_id])) == 8


def test_dead_run_resumes_once_from_its_step(env: Env, monkeypatch) -> None:
    hb = env.heartbeat("c", age=200, step=2, step_name="elaborate")
    spec_path = env.project.state / "demo" / f"{hb['run_id']}.spec.json"
    spec_path.write_text(json.dumps({"run_id": hb["run_id"], "start_at": {"stage": "synth", "checkpoint": None},
                                     "stages": [{"name": "synth", "cmd": "x", "resume": "x FIRST_STAGE={checkpoint}"}]}))
    starts: list[tuple] = []
    monkeypatch.setattr(launch, "start_driver", lambda ssh, host, driver, spec, log, env_: starts.append((host, driver, spec)) or 777)
    env.ssh.alive[4300] = True
    assert env.cycle()[hb["run_id"]] == "dead" and starts == [] and env.ssh.killed == []
    assert [e for e in env.events() if e[1] == "resume"] == [(hb["run_id"], "resume")]
    env.ssh.alive[4300] = False
    assert env.cycle()[hb["run_id"]] == "dead"
    assert json.loads(spec_path.read_text())["start_at"] == {"stage": "synth", "checkpoint": "elaborate"}
    assert starts == [("local", env.project.state / "bin" / "demo" / "edr_driver.py", spec_path)]
    rows = [tuple(r) for r in env.ledger.db.execute("SELECT stage, attempt, status FROM stage_runs WHERE run_id=? ORDER BY attempt", (hb["run_id"],))]
    assert rows == [("synth", 1, "running"), ("synth", 2, "resumed")]
    assert env.events().count((hb["run_id"], "resume")) == 2
    env.cycle(NOW + 1)
    assert len(starts) == 1


def test_queued_runs_are_relaunched(env: Env, monkeypatch) -> None:
    env.ledger.upsert_run({"run_id": rid("a"), "batch": "demo", "label": "a", "state": "queued"})
    calls: list[tuple] = []
    monkeypatch.setattr(launch, "launch", lambda project, batch, ssh, ledger, **kw: calls.append((batch.batch, kw)) or [])
    env.cycle()
    env.ledger.upsert_run({"run_id": rid("b"), "batch": "demo", "label": "b_nodw", "state": "queued"})
    env.cycle(NOW + 1)
    assert calls == [("demo", {"only": ["a"], "stagger_s": 0, "allow_dirty": True})] * 2  # one job per cycle


def test_orphans_are_reported_and_killed_only_when_asked(env: Env) -> None:
    env.project.limits.grace_s = 0
    hb = env.heartbeat("a")
    env.ssh.procs = [(99999, 120, 0.0, "sleep", "sleep 100"), (99998, 5, 0.0, "sleep", f"sleep 1 {hb['root']}")]
    states = env.cycle()
    assert states["orphan:local:99999"] == "orphan" and "orphan:local:99998" not in states
    assert env.events() == [("", "orphan")] and env.notifier.sent == [("orphan", "orphan:local:99999")]
    env.ssh.procs = [(99999, 121, 0.0, "sleep", "sleep 100")]
    env.cycle(NOW + 1)
    assert len(env.notifier.sent) == 1 and env.ssh.killed == []  # the age is not part of the alert text
    env.project.limits.kill_orphan = True
    env.ssh.procs = [(99997, 130, 0.0, "sleep", "sleep 200")]
    env.cycle(NOW + 1)
    assert env.ssh.killed == [("cmd", "kill -TERM 99997")] and ("", "kill") in env.events()


def test_dry_run_writes_nothing(env: Env, capsys) -> None:
    env.heartbeat("a")
    env.heartbeat("d", age=200)
    before = listing(env.project.state)
    states = env.cycle(dry_run=True)
    assert states == {rid("a"): "running", rid("d"): "dead"}
    assert listing(env.project.state) == before and not (env.project.data / "board").exists()
    assert [r["phase"] for r in env.ledger.runs()] == [None, None] and env.ledger.events() == []
    assert env.notifier.sent == []
    out = capsys.readouterr().out
    assert f"{rid('a')}: running" in out and f"{rid('d')}: dead" in out


def test_check_on_a_stale_watch_json(env: Env) -> None:
    env.project.limits.heartbeat_s = 5
    assert watch.check(env.project, [env.notifier]) == 1
    wj = env.project.state / "watch.json"
    wj.parent.mkdir(parents=True, exist_ok=True)
    wj.write_text(json.dumps({"ts": time.time(), "cycle": 3, "pid": 1}))
    assert watch.check(env.project, [env.notifier]) == 0
    wj.write_text(json.dumps({"ts": time.time() - 100, "cycle": 3, "pid": 1}))
    assert watch.check(env.project, [env.notifier]) == 1
    assert env.notifier.sent == [("watch", ""), ("watch", "")]


def test_stage_rows_follow_the_stages_map(env) -> None:
    """One-command stages get terminal rows with times; a stage left running in a finished run takes the run's class."""
    stages = {"synth": {"status": "done", "attempt": 1, "started": NOW - 3000, "ended": NOW - 2000, "exit": 0},
              "pnr": {"status": "done", "attempt": 1, "started": NOW - 2000, "ended": NOW - 1000, "exit": 0},
              "power": {"status": "done", "attempt": 1, "started": NOW - 1000, "ended": NOW - 10, "exit": 0}}
    hb = env.heartbeat("s1", phase="done", stage="power", exit=0, stages=stages)
    env.cycle()
    rows = {r[0]: tuple(r[1:]) for r in env.ledger.db.execute(
        "SELECT stage, status, started, ended, exit FROM stage_runs WHERE run_id=? AND task=''", (hb["run_id"],))}
    assert rows["synth"] == ("done", NOW - 3000, NOW - 2000, 0)
    assert rows["pnr"] == ("done", NOW - 2000, NOW - 1000, 0)
    failed = {"synth": {"status": "done", "attempt": 1, "started": 1, "ended": 2, "exit": 0},
              "pnr": {"status": "failed", "attempt": 1, "started": 2, "ended": 3, "exit": 5}}
    hb2 = env.heartbeat("s2", phase="FAILED:pnr", stage="pnr", exit=5, stages=failed)
    killed = {"synth": {"status": "running", "attempt": 1, "started": 1, "ended": None, "exit": None}}
    hb3 = env.heartbeat("s3", phase="KILLED:SIGTERM", stage="synth", exit=10, stages=killed)
    env.cycle()
    assert tuple(env.ledger.db.execute("SELECT status, exit FROM stage_runs WHERE run_id=? AND stage='pnr'",
                                       (hb2["run_id"],)).fetchone()) == ("failed", 5)
    assert env.ledger.db.execute("SELECT status FROM stage_runs WHERE run_id=? AND stage='synth'",
                                 (hb3["run_id"],)).fetchone()[0] == "killed"


def test_power_only_spec_collects_and_extracts_power_only(env: Env) -> None:
    hb = env.heartbeat("d", phase="done", exit=0, stage="power", tasks={"k_new": {"phase": "done"}})
    root = Path(hb["root"])
    for n in range(4):
        (root / "reports" / str(n)).mkdir(parents=True)
        (root / "reports" / str(n) / "area.rpt").write_text(f"i_top {1000 + n}\n")
    new = root / "simulation" / "tests" / "demo" / "NEW_TEST"
    (new / "power" / "reports").mkdir(parents=True)
    (new / "power" / "reports" / "power.csv").write_text("phase,total_w\nWHOLE,0.5\n")
    (new / "power" / "phases.json").write_text('{"window_ns": 10}\n')
    (env.project.state / "demo" / f"{hb['run_id']}.spec.json").write_text(json.dumps({"stages": [
        {"name": "power", "tasks": [{"id": "k_new", "dir": str(new)}]}]}))
    env.cycle()
    results = env.project.data / "results" / hb["run_id"]
    assert (results / "simulation" / "tests" / "demo" / "NEW_TEST" / "power" / "reports" / "power.csv").is_file()
    assert not (results / "reports").exists()
    rows = env.ledger.metrics(run_ids=[hb["run_id"]])
    assert {(r["stage"], r["task"], r["name"]) for r in rows} == {
        ("power", "k_new", "power_w"), ("power", "k_new", "window_ns"), ("power", "k_new", "energy_nj")}
    assert (hb["run_id"], "collect") not in env.events() and (hb["run_id"], "extract") not in env.events()


def test_run_forever_reloads_the_project_each_cycle(env: Env, monkeypatch) -> None:
    calls: list[str] = []

    def load(root):
        calls.append("load")
        if calls.count("load") == 2:
            raise config.ConfigError("edited mid-way")
        return env.project

    def sleep(_s):
        if calls.count("load") >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(config, "load_project", load)
    monkeypatch.setattr(watch, "cycle", lambda *a, **k: calls.append("cycle"))
    monkeypatch.setattr(watch.time, "sleep", sleep)
    env.notifier.project = None
    env.notifier.stop = lambda: None
    with pytest.raises(KeyboardInterrupt):
        watch.run_forever(env.project, env.ssh, env.ledger, [env.notifier])
    assert calls == ["load", "cycle", "load", "load", "cycle"]
    assert env.notifier.project is env.project

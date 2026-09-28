"""Tests of edarunner.watch: fake heartbeats in a tmp state dir, host local, a fake ssh, a recording notifier."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from helpers_watch import NEW, NOW, Env, rid

from edarunner import board, collect, config, launch, watch
from edarunner.backend import Live
from edarunner.hosts import Proc
from helpers_backend import FakeBackend


def listing(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def test_read_heartbeats_skips_retired_spec_keep_and_torn(env: Env) -> None:
    a = env.heartbeat("a")
    env.heartbeat("b", batch="old")
    (env.project.state_dir / "old" / "RETIRED").touch()
    d = env.project.state_dir / "demo"
    (d / f"{a['run_id']}.spec.json").write_text("{}")
    (d / f"{a['run_id']}.keep.json").write_text('{"hours": 1}')
    (d / f"{rid('torn')}.json").write_text('{"run_id": "torn", "phase')
    assert watch.read_heartbeats(env.project) == [("demo", a)]


def test_cycle_classifies_events_and_alerts(env: Env) -> None:
    a = env.heartbeat("a")
    env.heartbeat("a", batch="demo2", date=NEW, source="gdef5678")
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
    assert {r: env.db.run(r)["state"] for r in states} == states
    assert env.db.run(rid("a"))["counts"] == a["counts"] and env.db.run(rid("g"))["exit"] == 0
    assert sorted(env.events()) == sorted([(rid("a"), "superseded"), (rid("s"), "stale"), (rid("d"), "dead"),
                                           (rid("l"), "looping"), (rid("o"), "over_budget"), (rid("f"), "host_full"),
                                           (rid("g"), "done"), (rid("g"), "collect")])
    assert sorted(env.notifier.sent) == sorted([("superseded", rid("a")), ("dead", rid("d")), ("looping", rid("l")),
                                                ("over_budget", rid("o")), ("host_full", rid("f"))])
    assert len(env.notifier.boards) == 1
    assert env.notifier.boards[0].splitlines()[:2] == ["<b>Running</b>", "🔴 <code>d@demo</code> dead, synth 1/4 analyze, 1h"]
    looping = env.notifier.alerts[rid("l")]
    assert (looping.title, looping.who, looping.todo[0][1]) == ("same failure again in", "l@demo",
                                                                "edr stop l@demo --why looping")
    assert env.notifier.alerts[rid("d")].todo[0][1].startswith("edr continue d@demo --stage ")
    bdir = env.project.data / "board"
    assert {p.name for p in bdir.iterdir()} == {"board.json", "status.html", "compare.html"}
    assert set(env.db.get_store("progress")) == set(states) and rid("d") in env.db.get_store("notified")
    assert len(json.loads((bdir / "board.json").read_text())["runs"]) == 9
    probes = json.loads((bdir / "board.json").read_text())["hosts"]
    assert {s["host"] for s in env.db.host_samples(0)} == {h for h, p in probes.items() if "error" not in p} != set()
    assert f'<script src="{board.PLOTLY_URL}">' in (bdir / "compare.html").read_text()
    assert json.loads((env.project.state_dir / "watch.json").read_text())["cycle"] == 1
    n_events, n_sent = len(env.events()), len(env.notifier.sent)
    (bdir / board.PLOTLY_FILE).write_text("// a local copy\n")
    assert env.cycle(NOW + 1) == states
    assert f'<script src="{board.PLOTLY_FILE}">' in (bdir / "compare.html").read_text()
    assert (len(env.events()), len(env.notifier.sent)) == (n_events, n_sent)
    assert json.loads((env.project.state_dir / "watch.json").read_text())["cycle"] == 2
    assert listing(env.tmp / "scratch") == roots
    assert env.ssh.killed == [] and not list(env.project.state_dir.glob("*/*.stop"))


def test_hung_after_unchanged_progress_and_kill(env: Env) -> None:
    lim = env.project.limits
    lim.hung_s, lim.grace_s = 0, 0
    env.heartbeat("a")
    b = env.heartbeat("b", pgids=[4400])
    (env.project.state_dir / "demo" / f"{b['run_id']}.keep.json").write_text('{"hours": 0, "ack": true}')
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
    monkeypatch.setattr(launch, "stop", lambda ssh, db, run, hb, **kw: stops.append({**kw, "run_id": run["run_id"]}) or True)
    keep = env.project.state_dir / "demo" / f"{b['run_id']}.keep.json"
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
        env.heartbeat(label, batch="demo2", date=NEW, source="gdef5678")
    env.heartbeat("e", batch="demo2", date=NEW, source="gdef5678", phase="FAILED:synth", exit=5)
    (env.project.state_dir / "demo" / f"{k['run_id']}.keep.json").write_text('{"hours": 12}')
    states = env.cycle()
    assert states[rid("a")] == "superseded" and states[rid("k")] == "superseded"
    assert states[rid("f")] == "host_full" and states[rid("e")] == "running"
    stops = {p.name for p in (env.project.state_dir / "demo").glob("*.stop")}
    assert stops == {f"{a['run_id']}.stop", f"{f['run_id']}.stop"}
    assert (env.project.state_dir / "demo" / f"{a['run_id']}.stop").read_text().strip() == "after-task"
    assert (rid("a"), "stop") in env.events() and (rid("k"), "stop") not in env.events()
    n = env.events().count((rid("f"), "stop"))
    env.cycle(NOW + 1)
    assert env.events().count((rid("f"), "stop")) == n == 1


def test_collect_extract_and_parameters_once(env: Env, monkeypatch) -> None:
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
    rows = env.db.metrics(run_ids=[run_id])
    by = {(r["stage"], r["step"], r["name"]): r["value"] for r in rows}
    assert by[("synth", 3, "area_cell_um2")] == 1031.5 and by[("synth", 0, "wns_ns")] == 0.0
    assert len(by) == 8 and all(v is not None for v in by.values())
    params = {r["key"]: r["value"] for r in env.db.conn.execute("SELECT key, value FROM parameters WHERE run_id=?", (run_id,))}
    assert params == {"config": "demo", "DW": "0", "source": "gabc1234"}
    assert (run_id, "collect") not in env.events()
    env.cycle(NOW + 1)
    assert calls == [run_id] and len(env.db.metrics(run_ids=[run_id])) == 8


def test_a_failed_stage_yields_no_metrics(env: Env) -> None:
    hb = env.heartbeat("b_nodw", phase="FAILED:pnr", exit=5, stage="pnr", step=4, step_name="cts",
                       stages={"synth": {"status": "done", "exit": 0}, "pnr": {"status": "failed", "exit": 5}})
    root = Path(hb["root"])
    for n in range(6):  # a copied tree carries the pnr reports of another run
        (root / "reports" / str(n)).mkdir(parents=True)
        (root / "reports" / str(n) / "area.rpt").write_text(f"i_top {1000 + n}\n")
    env.cycle()
    assert (env.project.data / "results" / hb["run_id"] / "reports" / "5" / "area.rpt").is_file()
    rows = env.db.metrics(run_ids=[hb["run_id"]])
    assert [(r["stage"], r["step"]) for r in rows] == [("synth", 0), ("synth", 1), ("synth", 2), ("synth", 3)]


def test_dead_run_resumes_once_from_its_step(env: Env, monkeypatch) -> None:
    hb = env.heartbeat("c", age=200, step=2, step_name="elaborate")
    spec_path = env.project.state_dir / "demo" / f"{hb['run_id']}.spec.json"
    spec_path.write_text(json.dumps({"run_id": hb["run_id"], "start_at": {"stage": "synth", "checkpoint": None},
                                     "driver": "/x/bin/edr_driver-0badc0de.py",
                                     "stages": [{"name": "synth", "cmd": "x", "resume": "x FIRST_STAGE={checkpoint}"}]}))
    fake = FakeBackend()
    env.ssh.alive[4300] = True
    assert env.cycle(backend=fake)[hb["run_id"]] == "dead" and fake.requests == [] and env.ssh.killed == []
    assert [e for e in env.events() if e[1] == "resume"] == [(hb["run_id"], "resume")]
    env.ssh.alive[4300] = False
    assert env.cycle(backend=fake)[hb["run_id"]] == "dead"
    assert json.loads(spec_path.read_text())["start_at"] == {"stage": "synth", "checkpoint": "elaborate"}
    starts = [(r.host, str(r.driver), r.spec) for r in fake.requests]
    assert starts == [("local", "/x/bin/edr_driver-0badc0de.py", spec_path)]
    assert env.db.run(hb["run_id"])["handle"] == "fake:1"
    rows = [tuple(r) for r in env.db.conn.execute("SELECT stage, attempt, status FROM stage_runs WHERE run_id=? ORDER BY attempt", (hb["run_id"],))]
    assert rows == [("synth", 1, "running"), ("synth", 2, "resumed")]
    assert env.events().count((hb["run_id"], "resume")) == 2
    env.cycle(NOW + 1, backend=fake)
    assert len(fake.requests) == 1


def test_queued_runs_are_relaunched(env: Env, monkeypatch) -> None:
    env.db.upsert_run({"run_id": rid("a"), "batch": "demo", "label": "a", "state": "queued"})
    calls: list[tuple] = []
    monkeypatch.setattr(launch, "launch", lambda project, batch, ssh, db, **kw: calls.append((batch.batch, kw)) or [])
    env.cycle()
    env.db.upsert_run({"run_id": rid("b"), "batch": "demo", "label": "b_nodw", "state": "queued"})
    env.cycle(NOW + 1)
    assert calls == [("demo", {"only": ["a"], "stagger_s": 0, "allow_dirty": True})] * 2  # one job per cycle


def test_orphans_are_reported_and_killed_only_when_asked(env: Env) -> None:
    env.project.limits.grace_s = 0
    hb = env.heartbeat("a")
    env.ssh.procs = [Proc(99999, 120, 0.0, "sleep", "sleep 100"), Proc(99998, 5, 0.0, "sleep", f"sleep 1 {hb['root']}")]
    states = env.cycle()
    assert states["orphan:local:99999"] == "orphan" and "orphan:local:99998" not in states
    assert env.events() == [("", "orphan")] and env.notifier.sent == [("orphan", "orphan:local:99999")]
    env.ssh.procs = [Proc(99999, 121, 0.0, "sleep", "sleep 100")]
    env.cycle(NOW + 1)
    assert len(env.notifier.sent) == 1 and env.ssh.killed == []  # the age is not part of the alert text
    env.project.limits.kill_orphan = True
    env.ssh.procs = [Proc(99997, 130, 0.0, "sleep", "sleep 200")]
    env.cycle(NOW + 1)
    assert env.ssh.killed == [("cmd", "kill -TERM 99997")] and ("", "kill") in env.events()


def test_the_run_id_in_the_environment_decides_who_owns_a_tool(env: Env) -> None:
    live, dead, done = env.heartbeat("a")["run_id"], env.heartbeat("d", age=200)["run_id"], env.heartbeat(
        "g", phase="done", exit=0)["run_id"]
    env.ssh.procs = [Proc(501, 60, 99.0, "fc_shell", "fc_shell", "/home/me", live),
                     Proc(502, 60, 99.0, "fc_shell", "fc_shell", "/home/me", dead),
                     Proc(503, 60, 99.0, "fc_shell", "fc_shell", "/home/me", done),
                     Proc(504, 60, 99.0, "fc_shell", "fc_shell", "/scratch/x/edr/other/r9/pnr", "r9"),
                     Proc(505, 60, 99.0, "fc_shell", "fc_shell", "/scratch/x/edr/demo/r8/pnr", "r8"),
                     Proc(506, 60, 99.0, "fc_shell", "fc_shell -f /scratch/x/edr/other/r1/main.tcl", "/home/me"),
                     Proc(507, 60, 99.0, "fc_shell", "fc_shell", "/home/me")]
    states = env.cycle()
    assert sorted(k for k, s in states.items() if s == "orphan") == [f"orphan:local:{p}" for p in (502, 503, 505, 507)]
    you = "Your process fc_shell runs on local"
    assert {k.rsplit(":", 1)[1]: a.about.removesuffix(" It may hold a licence seat.")
            for k, a in env.notifier.alerts.items() if a.kind == "orphan"} == {
        "502": f"{you} for the run d@demo, whose driver is gone.",
        "503": f"{you} for the run g@demo, which has ended (done).",
        "505": f"{you} in the tree of the run r8, but this project has no record of that run.",
        "507": f"{you}, and no edarunner run owns it."}
    assert {e for e in env.events() if e[1] == "orphan"} == {(dead, "orphan"), (done, "orphan"), ("", "orphan")}


def test_dry_run_writes_nothing(env: Env, capsys) -> None:
    env.heartbeat("a")
    env.heartbeat("d", age=200)
    before = listing(env.project.state_dir)
    states = env.cycle(dry_run=True)
    assert states == {rid("a"): "running", rid("d"): "dead"}
    assert listing(env.project.state_dir) == before and not (env.project.data / "board").exists()
    assert [r["phase"] for r in env.db.runs()] == [None, None] and env.db.events() == []
    assert env.notifier.sent == []
    out = capsys.readouterr().out
    assert f"{rid('a')}: running" in out and f"{rid('d')}: dead" in out


def test_check_on_a_stale_watch_json(env: Env) -> None:
    env.project.limits.heartbeat_s = 5
    assert watch.check(env.project, [env.notifier]) == 1
    wj = env.project.state_dir / "watch.json"
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
    rows = {r[0]: tuple(r[1:]) for r in env.db.conn.execute(
        "SELECT stage, status, started, ended, exit FROM stage_runs WHERE run_id=? AND task=''", (hb["run_id"],))}
    assert rows["synth"] == ("done", NOW - 3000, NOW - 2000, 0)
    assert rows["pnr"] == ("done", NOW - 2000, NOW - 1000, 0)
    failed = {"synth": {"status": "done", "attempt": 1, "started": 1, "ended": 2, "exit": 0},
              "pnr": {"status": "failed", "attempt": 1, "started": 2, "ended": 3, "exit": 5}}
    hb2 = env.heartbeat("s2", phase="FAILED:pnr", stage="pnr", exit=5, stages=failed)
    killed = {"synth": {"status": "running", "attempt": 1, "started": 1, "ended": None, "exit": None}}
    hb3 = env.heartbeat("s3", phase="KILLED:SIGTERM", stage="synth", exit=10, stages=killed)
    env.cycle()
    assert tuple(env.db.conn.execute("SELECT status, exit FROM stage_runs WHERE run_id=? AND stage='pnr'",
                                       (hb2["run_id"],)).fetchone()) == ("failed", 5)
    assert env.db.conn.execute("SELECT status FROM stage_runs WHERE run_id=? AND stage='synth'",
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
    (env.project.state_dir / "demo" / f"{hb['run_id']}.spec.json").write_text(json.dumps({"stages": [
        {"name": "power", "tasks": [{"id": "k_new", "dir": str(new)}]}]}))
    env.cycle()
    results = env.project.data / "results" / hb["run_id"]
    assert (results / "simulation" / "tests" / "demo" / "NEW_TEST" / "power" / "reports" / "power.csv").is_file()
    assert not (results / "reports").exists()
    rows = env.db.metrics(run_ids=[hb["run_id"]])
    assert {(r["stage"], r["task"], r["name"]) for r in rows} == {
        ("power", "k_new", "power_w"), ("power", "k_new", "window_ns"), ("power", "k_new", "energy_nj")}
    assert (hb["run_id"], "collect") not in env.events() and (hb["run_id"], "extract") not in env.events()


def test_run_forever_goes_on_with_the_last_good_config(env: Env, monkeypatch) -> None:
    calls: list = []
    errors = iter(["", "edited mid-way", "edited mid-way", "", "edited again"])

    def load(root):
        calls.append("load")
        if error := next(errors):
            raise config.ConfigError(error)
        return env.project

    def sleep(_s):
        if calls.count("load") >= 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(config, "load_project", load)
    monkeypatch.setattr(watch, "cycle", lambda *a, start=True, **k: calls.append(start))
    monkeypatch.setattr(watch.time, "sleep", sleep)
    env.notifier.project = None
    env.notifier.stop = lambda: None
    with pytest.raises(KeyboardInterrupt):
        watch.run_forever(env.project, env.ssh, env.db, [env.notifier])
    assert calls == ["load", True, "load", False, "load", False, "load", True, "load", False]
    assert env.notifier.sent == [("config", "demo"), ("config", "demo")]
    alert = env.notifier.alerts["demo"]
    assert (alert.title, alert.code, alert.todo[0][1]) == ("config does not load", "edited again", "edr check")
    assert env.notifier.project is env.project


def test_a_cycle_that_may_not_start_resumes_and_launches_nothing(env: Env, monkeypatch) -> None:
    hb = env.heartbeat("c", age=200, step=2, step_name="elaborate")
    (env.project.state_dir / "demo" / f"{hb['run_id']}.spec.json").write_text(json.dumps({
        "run_id": hb["run_id"], "driver": "/x/bin/edr_driver-0badc0de.py",
        "stages": [{"name": "synth", "cmd": "x", "resume": "x FIRST_STAGE={checkpoint}"}]}))
    env.db.upsert_run({"run_id": rid("q"), "batch": "demo", "label": "a", "state": "queued"})
    launches: list = []
    monkeypatch.setattr(launch, "launch", lambda *a, **k: launches.append(a) or [])
    fake = FakeBackend()
    assert env.cycle(backend=fake, start=False)[hb["run_id"]] == "dead"
    assert fake.requests == [] and launches == [] and ("dead", hb["run_id"]) in env.notifier.sent
    env.cycle(NOW + 1, backend=fake)
    assert len(fake.requests) == 1 and len(launches) == 1


def test_run_forever_once_returns_1_when_the_cycle_failed(env: Env, monkeypatch) -> None:
    def boom(*a, **k):
        raise KeyError("runs: unknown columns")

    monkeypatch.setattr(config, "load_project", lambda root: env.project)
    monkeypatch.setattr(watch, "cycle", lambda *a, **k: None)
    assert watch.run_forever(env.project, env.ssh, env.db, [env.notifier], once=True) == 0
    monkeypatch.setattr(watch, "cycle", boom)
    assert watch.run_forever(env.project, env.ssh, env.db, [env.notifier], once=True) == 1


def test_stale_leases_of_this_project_are_swept_with_an_event(env: Env, user_root: Path) -> None:
    env.heartbeat("live")
    env.heartbeat("gone", phase="KILLED:SIGKILL", exit=10)
    env.heartbeat("d", age=200)
    env.heartbeat("moved", stage="pnr")
    d = user_root / "leases" / "demo"
    d.mkdir(parents=True)

    def lease(label: str, age: float, budget_s: float | None = None, run_id: str | None = None,
              project: str = "demo") -> str:
        run_id, name = run_id or rid(label), f"{project}.{run_id or rid(label)}.synth.0"
        (d / name).write_text(json.dumps({"project": project, "run_id": run_id, "key": f"{project}.{run_id}.synth",
                                          "stage": "synth", "pid": 4242, "host": "local", "ts": NOW - age,
                                          "budget_s": budget_s}))
        return name

    keep = [lease("live", 300, 3600), lease("young", 10, run_id=rid("unknown")), lease("other", 300, project="beta")]
    lease("gone", 300)
    lease("d", 300)
    lease("moved", 300)
    lease("other", 300)
    (d / f".demo.{rid('live')}.synth.1.tmp").write_text("{")
    assert len(env.cycle(dry_run=True)) == 4 and len(list(d.iterdir())) == 8
    env.cycle()
    assert sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) == sorted(keep)
    texts = {e["run_id"]: e["text"] for e in env.db.events(n=500) if e["kind"] == "lease"}
    assert texts == {
        rid("gone"): f"stale lease demo/demo.{rid('gone')}.synth.0: the run ended KILLED:SIGKILL",
        rid("d"): f"stale lease demo/demo.{rid('d')}.synth.0: the run is dead",
        rid("moved"): f"stale lease demo/demo.{rid('moved')}.synth.0: the run left stage synth",
        rid("other"): f"stale lease demo/demo.{rid('other')}.synth.0: no live heartbeat of the run"}
    lease("live", 3601, 3600)
    env.cycle(NOW + 1)
    assert not (d / f"demo.{rid('live')}.synth.0").exists()
    assert any(e["text"] == f"stale lease demo/demo.{rid('live')}.synth.0: older than the stage budget of 3600 s"
               for e in env.db.events(n=50))


@pytest.mark.parametrize("age, live, state, reason", [
    (200, Live.GONE, "dead", "heartbeat older than 90 s, driver 4242 gone on local"),
    (200, Live.RUNNING, "stale", "heartbeat older than 90 s, driver 4242 alive"),
    (200, Live.SUSPENDED, "suspended", "suspended by the scheduler"),
    (200, Live.UNKNOWN, "stale", "heartbeat older than 90 s, local did not answer"),
    (60, Live.SUSPENDED, "suspended", "suspended by the scheduler"),
    (5, Live.HELD, "held", "held by the scheduler"),
    (60, Live.RUNNING, "stale", "heartbeat older than 30 s"),
    (5, Live.GONE, "running", None),  # a fresh heartbeat outweighs the backend: the file may lag on NFS
])
def test_the_backend_state_of_a_live_run(env: Env, age, live, state, reason) -> None:
    fake = FakeBackend()
    hb = env.heartbeat("a", age=age)
    fake.states["local:4242"] = [live]
    assert env.cycle(backend=fake)[hb["run_id"]] == state
    events = [e["text"] for e in env.db.events(run_id=hb["run_id"]) if e["kind"] == state]
    assert events == ([reason] if reason else [])


def test_the_backend_is_asked_once_per_cycle_for_every_live_run(env: Env) -> None:
    fake = FakeBackend()
    env.heartbeat("a")
    env.heartbeat("b", driver_pid=None)
    env.db.upsert_run({"run_id": rid("b"), "handle": "fake:7"})
    env.heartbeat("c", driver_pid=None)
    env.heartbeat("g", phase="done", exit=0)
    fake.states["7"] = [Live.RUNNING, Live.GONE]
    env.cycle(backend=fake)
    env.cycle(NOW + 1, backend=fake)
    assert [sorted(ids) for ids in fake.asked] == [["7", "local:4242"]] * 2


def test_hung_reads_the_samples_of_the_heartbeat(env: Env) -> None:
    lim = env.project.limits
    lim.hung_s, lim.grace_s = 0, 3600
    env.heartbeat("a", cpu_s=1.5, log_bytes=100)
    assert env.cycle()[rid("a")] == "running"
    env.heartbeat("a", cpu_s=2.5, log_bytes=100)
    assert env.cycle(NOW + 1)[rid("a")] == "running"
    assert env.cycle(NOW + 2)[rid("a")] == "hung"


def test_a_scheduler_job_before_its_first_heartbeat(env: Env) -> None:
    fake = FakeBackend()
    fake.name = "condor"
    env.db.upsert_run({"run_id": rid("p"), "batch": "demo", "label": "p", "phase": "setup", "handle": "condor:1.0"})
    env.db.upsert_run({"run_id": rid("q"), "batch": "demo", "label": "q", "state": "queued"})
    fake.states["1.0"] = [Live.PENDING, Live.HELD, Live.GONE]
    assert env.cycle(backend=fake)[rid("p")] == "pending"
    assert env.cycle(NOW + 1, backend=fake)[rid("p")] == "held"
    assert env.notifier.sent == [("held", rid("p"))]
    assert env.cycle(NOW + 2, backend=fake)[rid("p")] == "failed"
    assert env.db.run(rid("p"))["phase"] == "FAILED:scheduler" and rid("q") not in env.cycle(NOW + 3, backend=fake)
    assert fake.asked == [["1.0"]] * 3

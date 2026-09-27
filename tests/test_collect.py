"""Tests of edarunner.collect on the local host against a fake run tree of the demo layout."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from edarunner import collect
from edarunner.config import load_project
from edarunner.hosts import Ssh
from edarunner.db import Database

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
RUN_ID = "20260926_1200_a_demo_gabc1234"
TESTS = ("GEMM_M64_N64", "SOFTMAX_R197")


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def run_tree(root: Path, steps: range = range(4)) -> None:
    """What flow.sh and kernel.sh leave behind after synth and two tasks."""
    for n in steps:
        write(root / "reports" / str(n) / "area.rpt", f"i_top {1000 + n * 10.5}\n")
        write(root / "reports" / str(n) / "qor.rpt", f"Critical Path Slack: -0.0{n}\n")
    for log in ("synth", "pnr", "export", "power.k_small", "power.k_big"):
        write(root / "log" / f"{log}.log")
    for test in TESTS:
        d = root / "simulation" / "tests" / "demo" / test
        write(d / "power" / "reports" / "power.csv", "phase,total_w\nWHOLE,0.250\n")
        write(d / "power" / "phases.json", '{"window_ns": 3400}\n')
        write(d / "wave.vcd", "vcd")
    write(root / "out" / "11" / "netlist.v", "module top; endmodule\n")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = replace(load_project(DEMO), data=tmp_path / "data")
    root = tmp_path / "scratch" / "edr" / "demo" / RUN_ID
    run = {"run_id": RUN_ID, "batch": "demo", "label": "a", "config": "demo", "host": "local", "root": str(root)}
    with Database(tmp_path / "data" / "edr.db") as db:
        yield project, Ssh(project.site), db, run, root


def heartbeat(run: dict, phase: str, stage: str, tasks: dict[str, str] | None = None) -> dict:
    return {**run, "phase": phase, "stage": stage, "tasks": {t: {"phase": p} for t, p in (tasks or {}).items()}}


def artifacts(db: Database) -> list[tuple[str, str, int]]:
    return [tuple(r) for r in db.conn.execute("SELECT path, class, bytes FROM artifacts ORDER BY path")]


def test_done_run_and_idempotent(env) -> None:
    project, ssh, db, run, root = env
    run_tree(root)
    hb = heartbeat(run, "done", "power", {"k_small": "done", "k_big": "done"})
    r = collect.collect_run(project, ssh, db, run, hb)
    assert r.failures == []
    results = project.data / "results" / RUN_ID
    assert (results / "reports" / "3" / "area.rpt").read_text() == "i_top 1031.5\n"
    assert (results / "log" / "power.k_big.log").is_file()
    for test in TESTS:
        assert (results / "simulation" / "tests" / "demo" / test / "power" / "reports" / "power.csv").is_file()
        assert not (results / "simulation" / "tests" / "demo" / test / "wave.vcd").exists()
    assert not (results / "out").exists()
    assert r.files == 8 + 5 + 4 == len(r.copied)
    assert "reports/0/area.rpt" in r.copied and "log/synth.log" in r.copied
    rows = artifacts(db)
    assert len(rows) == r.files and {c for _, c, _ in rows} == {"always"}
    assert ("reports/3/area.rpt", "always", 13) in rows

    again = collect.collect_run(project, ssh, db, run, hb)
    assert (again.files, again.failures, again.copied) == (0, [], [])
    assert artifacts(db) == rows


def test_running_stage_copies_final_steps_only(env) -> None:
    project, ssh, db, run, root = env
    run_tree(root)
    old = time.time() - 3600
    for n in (0, 1):
        os.utime(root / "reports" / str(n), (old, old))
    hb = heartbeat(run, "stage:synth", "synth")
    r = collect.collect_run(project, ssh, db, run, hb, step_final_s=600)
    assert r.failures == []
    results = project.data / "results" / RUN_ID
    assert sorted(p.name for p in (results / "reports").iterdir()) == ["0", "1"]
    assert (results / "log" / "synth.log").is_file()
    assert not (results / "simulation").exists()


def test_missing_path_is_a_counted_failure(env) -> None:
    project, ssh, db, run, root = env
    write(root / "log" / "synth.log")
    hb = heartbeat(run, "FAILED:pnr", "pnr")
    r = collect.collect_run(project, ssh, db, run, hb)
    assert len(r.failures) == 1 and r.failures[0].startswith("reports/: rsync rc 23")
    assert r.copied == ["log/synth.log"]


def test_dry_run_writes_nothing(env) -> None:
    project, ssh, db, run, root = env
    run_tree(root)
    hb = heartbeat(run, "done", "power", {"k_small": "done", "k_big": "failed"})
    r = collect.collect_run(project, ssh, db, run, hb, dry_run=True)
    assert r.failures == [] and r.files == 17
    assert "simulation/tests/demo/SOFTMAX_R197/power/phases.json" in r.copied
    assert not (project.data / "results").exists()
    assert artifacts(db) == []


def test_on_request(env) -> None:
    project, ssh, db, run, root = env
    run_tree(root)
    r = collect.collect_on_request(project, ssh, db, run, "netlist")
    assert r.failures == [] and r.copied == ["out/11/netlist.v"]
    assert artifacts(db) == [("out/11/netlist.v", "netlist", 22)]
    assert collect.collect_on_request(project, ssh, db, run, "nope").failures == ["no stage has collect_on_request.nope"]


def test_nfs_export_fallback(env, monkeypatch) -> None:
    project, ssh, db, run, root = env
    run_tree(root)
    project = replace(project, site=replace(project.site, nfs_export="{root}"))
    real = collect.subprocess.run
    calls: list[list[str]] = []

    def fake(argv, **kw):
        calls.append(argv)
        if "-e" in argv:
            return real(["sh", "-c", "exit 255"], **kw)
        return real(argv, **kw)

    monkeypatch.setattr(collect.subprocess, "run", fake)
    run_far = {**run, "host": "far"}
    hb = heartbeat(run_far, "done", "synth")
    r = collect.collect_run(project, ssh, db, run_far, hb)
    assert r.failures == [] and r.files == 13
    assert any("far:" in a[-2] for a in calls) and any(a[-2] == f"{root}/reports/" for a in calls)


def test_spec_tasks_and_stages_win_over_the_task_table(env) -> None:
    project, ssh, db, run, root = env
    run_tree(root)
    new = root / "simulation" / "tests" / "demo" / "NEW_TEST"
    write(new / "power" / "reports" / "power.csv", "phase,total_w\nWHOLE,0.5\n")
    write(new / "power" / "phases.json", '{"window_ns": 10}\n')
    spec_dir = project.state / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"{RUN_ID}.spec.json").write_text(json.dumps({"stages": [
        {"name": "power", "tasks": [{"id": "k_new", "dir": str(new)}]}]}))
    hb = heartbeat(run, "done", "power", {"k_new": "done"})
    r = collect.collect_run(project, ssh, db, run, hb)
    assert r.failures == [], r.failures
    results = project.data / "results" / RUN_ID
    assert (results / "simulation" / "tests" / "demo" / "NEW_TEST" / "power" / "reports" / "power.csv").is_file()
    # The tree's synth reports belong to the run that made them, not to this power-only run.
    assert not (results / "reports").exists()
    assert collect.spec_task_dirs({"stages": [{"name": "power", "tasks": [{"id": "k_new", "dir": str(new)}]}]},
                                  str(root)) == {"k_new": "simulation/tests/demo/NEW_TEST"}

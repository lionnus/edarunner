"""cli.py: every command on a copy of examples/local-demo in tmp_path, host local, no driver started."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import pytest
from helpers_cli import DATE, bdir, dead_pid, edr, seed
from helpers_results import demo_qor

from edarunner import board, cli, config, home, launch, serve, watch
from edarunner import db as db_mod
from edarunner.db import Database
from edarunner.guards import Refuse
from edarunner.hosts import HostError, HostProbe, Ssh
from edarunner.model import Telegram
from edarunner.notify import Notifier
from edarunner.notify.telegram import TelegramBot
from helpers_driver import DEMO


def with_vars(root: Path, run_id: str) -> None:
    """The spec of a seeded run with the job vars of the demo job `a`."""
    (bdir(root) / f"{run_id}.spec.json").write_text(json.dumps({"vars": {"netlist_stage": "11"}}))


def add_metric(root: Path, run_id: str, name: str, value: float, step: int | None = 3, task: str = "",
               stage: str = "synth") -> None:
    with Database(config.load_project(root).data / "edr.db") as db:
        db.add_metric({"run_id": run_id, "stage": stage, "step": step, "task": task, "name": name,
                        "canonical": "design__instance__area" if name == "area_cell_um2" else "", "value": value, "unit": "u"})


def keep_file(root: Path, run_id: str) -> dict:
    return json.loads((bdir(root) / f"{run_id}.keep.json").read_text())


def beat(root: Path, run_id: str, **fields) -> None:
    """Set `fields` in the heartbeat of a seeded run."""
    path = bdir(root) / f"{run_id}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


DEMO_STAGES = ("synth", "pnr", "export", "power")


def ran(demo: Path, run_id: str, phase: str, stages: dict[str, tuple[str, int | None]], names: tuple[str, ...] = DEMO_STAGES,
        t0: int = 100, tree: str = "") -> None:
    """A seeded run launched with the stages `names`, whose heartbeat has ended `stages` as (status, exit); `tree` is
    the root of the run when it works on the tree of another."""
    (bdir(demo) / f"{run_id}.spec.json").write_text(json.dumps({"stages": [{"name": n} for n in names]}))
    path = bdir(demo) / f"{run_id}.json"
    hb = json.loads(path.read_text())
    hb.update(phase=phase, stage=list(stages)[-1], root=tree or hb["root"], stages={
        n: {"status": s, "attempt": 1, "started": t0 + i, "ended": t0 + i + 1, "exit": e} for i, (n, (s, e)) in
        enumerate(stages.items())})
    path.write_text(json.dumps(hb))


# init and check


def test_init_writes_edr_toml_once(tmp_path: Path, monkeypatch, capsys) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    code, out, _ = edr(capsys, "init", "--site", str(DEMO))
    assert code == 0 and "edr.toml" in out
    text = (fresh / "edr.toml").read_text()
    assert f'site = "{DEMO / "site.toml"}"' in text and 'project = "fresh"' in text and "$project" not in text
    assert sorted(p.name for p in fresh.iterdir()) == ["edr.toml"]
    project = config.load_project(fresh)
    assert project.project == "fresh" and project.site.path == DEMO / "site.toml" and "synth" in project.stages
    code, _, err = edr(capsys, "init", "--site", str(DEMO))
    assert code == 1 and "exists" in err
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    code, out, _ = edr(capsys, "init", "--site", str(DEMO / "site.toml"), "--dry-run")
    assert code == 0 and "(dry)" in out and list(other.iterdir()) == []


def test_check_reports_problems(demo: Path, capsys) -> None:
    code, out, _ = edr(capsys, "check")
    assert code == 0 and out.startswith("ok:") and "1 hosts" in out and "1 batches" in out
    edr_toml = demo / "edr.toml"
    edr_toml.write_text(edr_toml.read_text().replace('ref = "HEAD"', 'ref = "HEAD"\nbuild_tag = "python:hooks/nope.py:tag"'))
    code, out, _ = edr(capsys, "--json", "check")
    data = json.loads(out)["data"]
    # The hook check names it once; the plan of each of the two jobs names it again.
    assert code == 1 and len(data["problems"]) == 3 and "nope.py" in data["problems"][0] and data["hosts"][0]["host"] == "local"
    edr_toml.write_text(edr_toml.read_text().replace('\nbuild_tag = "python:hooks/nope.py:tag"', "")
                        .replace("export {run_id} {config}", "export {run_id} {config} {nope}"))
    jobs = demo / "jobs" / "demo.toml"
    jobs.write_text(jobs.read_text().replace('label = "b_nodw"\nconfig = "demo"\nhost = "local"', 'label = "b_nodw"\nconfig = "demo"\nhost = "mars"'))
    code, out, _ = edr(capsys, "check")
    assert code == 1 and "unknown host mars" in out and "nope" not in out
    jobs.write_text(jobs.read_text().replace('host = "mars"', 'host = "local"'))
    code, out, _ = edr(capsys, "check")
    assert code == 1 and "demo/a: unknown placeholder {nope}" in out


def test_check_warns_about_a_database_on_nfs(demo: Path, capsys, monkeypatch) -> None:
    code, out, _ = edr(capsys, "check")
    assert "warning" not in out
    monkeypatch.setattr(db_mod, "fs_magic", lambda path: 0x6969)
    code, out, _ = edr(capsys, "--json", "check")
    data = json.loads(out)["data"]
    assert data["warnings"] == [f"{demo / 'data' / 'edr.db'} is on a network filesystem (nfs); journal_mode DELETE, not WAL"]


# status, events, handles


def test_status_boards_handles_live_and_triage(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    q = seed(demo, "q", None, tree=False, state="queued")
    code, out, _ = edr(capsys, "status")
    assert code == 0 and "b_nodw" in out and "done" in out and out.startswith("#")
    with Database(demo / "data" / "edr.db") as db:
        last = db.get_store("last_board")
    assert last == [r["run_id"] for r in board.order([{"run_id": a, "phase": "done"}, {"run_id": b, "phase": "stage:synth", "label": "b_nodw"}, {"run_id": q, "state": "queued", "label": "q"}])]
    code, out, _ = edr(capsys, "status", "#1")
    assert code == 0 and out.startswith(last[0])
    code, out, _ = edr(capsys, "status", "a@demo")
    assert code == 0 and out.startswith(a) and "done" in out and "step 2 elaborate" in out
    code, out, _ = edr(capsys, "status", a[:20])
    assert code == 0 and out.startswith(a)
    code, out, _ = edr(capsys, "status", "--narrow")
    assert code == 0 and all(len(ln) <= 48 for ln in out.splitlines()) and "b_nodw" in out
    code, out, _ = edr(capsys, "--json", "status")
    rows = json.loads(out)["data"]["runs"]
    assert code == 0 and {r["run_id"] for r in rows} == {a, b, q} and json.loads(out)["output"] == ""
    code, out, _ = edr(capsys, "--json", "status", "--live")
    rows = {r["run_id"]: r for r in json.loads(out)["data"]["runs"]}
    assert code == 0 and rows[b]["alive"] is False and rows[b]["state"] == "dead" and "alive" not in rows[a]
    code, out, _ = edr(capsys, "status", "--triage")
    assert code == 0 and f"edr export --source abc1234" in out and f"edr launch demo --only q" in out
    assert "b_nodw@demo" not in out  # fresh heartbeat: running, nothing to triage
    code, out, _ = edr(capsys, "status", "--triage", "--live")
    assert code == 0 and f"edr continue b_nodw@demo --stage synth --from elaborate" in out
    code, out, _ = edr(capsys, "status", "--batch", "other")
    assert code == 0 and out == "no runs\n"
    code, _, err = edr(capsys, "status", "nope@demo")
    assert code == 1 and "nope" in err
    code, _, err = edr(capsys, "status", "#9")
    assert code == 1


def test_the_triage_stops_a_live_run_over_its_budget_and_continues_one_that_ended(demo: Path, capsys) -> None:
    seed(demo, "b_nodw", "stage:pnr", state="over_budget")
    a = seed(demo, "a", "OVER_BUDGET:pnr")
    ran(demo, a, "OVER_BUDGET:pnr", {"synth": ("done", 0), "pnr": ("over_budget", 0)})
    z = seed(demo, "z", "OVER_BUDGET:power")
    ran(demo, z, "OVER_BUDGET:power", {n: ("done", 0) for n in DEMO_STAGES})  # power is the last stage
    code, out, _ = edr(capsys, "status", "--triage")
    assert code == 0 and "    edr stop b_nodw@demo --why over-budget\n" in out and "    edr continue a@demo\n" in out
    assert "z@demo" not in out  # no stage left, so nothing to propose
    decisions = json.loads(edr(capsys, "--json", "brief")[1])["data"]["decisions"]
    assert [(d["handle"], d["command"]) for d in decisions] == [
        ("b_nodw@demo", "edr stop b_nodw@demo --why over-budget"), ("a@demo", "edr continue a@demo")]
    assert "The triage proposes `edr continue a@demo`." in edr(capsys, "brief", "--run", "a@demo")[1]
    assert "The triage proposes nothing for it." in edr(capsys, "brief", "--run", "z@demo")[1]
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert "<code>edr continue a@demo</code>" in acts.status_text("a@demo") and "<code>edr " not in acts.status_text("z@demo")


def test_the_triage_continues_a_stopped_run_with_work_left_and_retires_one_without(demo: Path, capsys) -> None:
    a = seed(demo, "a", "STOPPED")
    ran(demo, a, "STOPPED", {"synth": ("done", 0), "pnr": ("done", 0)})  # export and power are left
    h = seed(demo, "h", "STOPPED")
    ran(demo, h, "STOPPED", {n: ("done", 0) for n in DEMO_STAGES})
    beat(demo, h, tasks={"power": {"k_small": {"phase": "done", "started": 110, "ended": 120, "exit": 0},
                                   "k_big": {"phase": "held"}}})
    k = seed(demo, "k", "STOPPED")
    ran(demo, k, "STOPPED", {"synth": ("done", 0), "pnr": ("stopped", None)})  # a stop --now in pnr: continue refuses
    z = seed(demo, "z", "STOPPED")
    ran(demo, z, "STOPPED", {n: ("done", 0) for n in DEMO_STAGES})
    code, out, _ = edr(capsys, "status", "--triage")
    assert code == 0 and "    edr continue a@demo\n" in out and "    edr continue h@demo\n" in out
    assert "    edr retire z@demo --why STOPPED\n" in out and "k@demo" not in out
    decisions = json.loads(edr(capsys, "--json", "brief")[1])["data"]["decisions"]
    assert [(d["handle"], d["command"]) for d in decisions] == [
        ("a@demo", "edr continue a@demo"), ("h@demo", "edr continue h@demo"), ("z@demo", "edr retire z@demo --why STOPPED")]
    assert "The triage proposes `edr continue h@demo`." in edr(capsys, "brief", "--run", "h@demo")[1]
    assert "The triage proposes nothing for it." in edr(capsys, "brief", "--run", "k@demo")[1]
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert "<code>edr continue a@demo</code>" in acts.status_text("a@demo")
    assert "<code>edr retire z@demo --why STOPPED</code>" in acts.status_text("z@demo")
    assert "<code>edr " not in acts.status_text("k@demo")


def test_a_heartbeat_that_cannot_be_read_shows_its_run_unreadable_and_the_others_as_usual(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    x = seed(demo, "x", "done")
    beat(demo, x, tasks={"k_small": {"stage": "power", "phase": "done"}})  # the flat map of an older driver
    error = "AttributeError: 'str' object has no attribute 'get'"
    code, out, _ = edr(capsys, "--json", "status")
    states = {r["run_id"]: (board.state_of(r), r.get("error")) for r in json.loads(out)["data"]["runs"]}
    assert code == 0 and states == {a: ("done", None), x: ("unreadable", error)}
    assert "    edr status x@demo\n" in edr(capsys, "status", "--triage")[1]
    code, out, _ = edr(capsys, "status", "x@demo")
    assert code == 0 and f"unreadable: {error}" in out
    decisions = json.loads(edr(capsys, "--json", "brief")[1])["data"]["decisions"]
    assert ("x@demo", "edr status x@demo") in [(d["handle"], d["command"]) for d in decisions]
    assert f"such as a heartbeat that an older driver wrote ({error})" in edr(capsys, "brief", "--run", "x@demo")[1]
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert "🔴 <code>x@demo</code> unreadable" in acts.status_text(everything=True)
    assert f"unreadable: {error}" in acts.status_text("x@demo")
    with Database(demo / "data" / "edr.db") as db:
        db.upsert_run({"run_id": x, "state": "unreadable"})  # as the watcher leaves it
    beat(demo, x, tasks={})  # once the heartbeat reads again, so does the run
    run = json.loads(edr(capsys, "--json", "status", "x@demo")[1])["data"]["run"]
    assert board.state_of(run) == "done" and "error" not in run


def test_events_filters(demo: Path, capsys) -> None:
    code, out, _ = edr(capsys, "events")
    assert code == 2 and out == "no events\n"
    a = seed(demo, "a", "done")
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("user", a, "launch", "local /x")
        db.add_event("watch", a, "done", "")
        db.add_event("user", "", "export", "x -> y")
    code, out, _ = edr(capsys, "events", "-n", "2")
    assert code == 0 and len(out.splitlines()) == 4 and "launch" not in out and "export" in out
    code, out, _ = edr(capsys, "events", "--run", "a@demo")
    assert code == 0 and len(out.splitlines()) == 4 and re.search(r"a@demo +launch +local /x", out)
    code, out, _ = edr(capsys, "--json", "events", "--since", "1h")
    assert code == 0 and len(json.loads(out)["data"]) == 3
    code, out, _ = edr(capsys, "events", "--since", "1")
    assert code in (0, 2)
    code, _, err = edr(capsys, "events", "--since", "soon")
    assert code == 1 and "--since" in err


# brief


def test_brief_on_an_empty_project_names_it_and_says_no_runs(demo: Path, capsys) -> None:
    code, out, _ = edr(capsys, "brief")
    assert code == 0 and out.startswith("# demo\n") and f"`{demo}`" in out and "There are no runs yet" in out
    assert "No census has probed the hosts yet" in out and not (demo / "data" / "edr.db").exists()


def test_brief_has_its_sections_in_order(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    seed(demo, "b", "FAILED:synth", exit=5)
    seed(demo, "c", "stage:synth")
    (demo / "wt" / "abc1234").mkdir(parents=True)
    (demo / "wt" / "def5678").mkdir(parents=True)
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("user", a, "launch", "local /x")
    probe = HostProbe("local", 0.2, 6.0, "/x", 90.0, cores=4, load=3.8, total_ram_gb=8.0, total_gb=100.0)
    config.save_json(home.root() / "census.json", {"ts": time.time() - 600, "hosts": {"local": asdict(probe)}, "runs": [
        {"project": "demo", "host": "local", "cpu_pct": 350.0, "rss_gb": 1.0, "tree_gb": 2.0}]})
    (demo / "AGENTS.md").write_text("notes\n")
    code, out, _ = edr(capsys, "brief")
    heads = [ln for ln in out.splitlines() if ln.startswith("## ")]
    assert code == 0 and heads == ["## The flow", "## The site", "## The state", "## Read more"]
    assert "- `synth` runs one command through 4 steps, collects `reports/` and needs the tool `demo`." in out
    assert "- `power` is a task group that runs 2 tasks at a time" in out
    assert "The hosts at the census 10 minutes ago, the ones where a run of this project can start first" in out
    assert ("- 🔴 `local`: 0.2 of 4 cores, 6 of 8 GB RAM and 90 of 100 GB scratch are free. No run can start there: "
            "0.2 cores free, a run needs 1. Your runs there: demo 1, using 3.5 cores, 1 GB RAM and 2 GB scratch. "
            "Your runs use 3.5 of its 3.8 busy cores.") in out
    assert "Batch `demo` has 3 runs: 1 failed, 1 running and 1 done. Its source is `abc1234` (lag unknown)." in out
    assert "2 sources are checked out under" in out
    assert "\n- `abc1234`, used by batch `demo`\n- `def5678`, used by no batch\n" in out
    assert "One run has not finished:" in out and "`c@demo` is running in stage `synth` at step 2 (elaborate) on `local`" in out
    assert "`b@demo` (failed): `edr retire b@demo --why FAILED:synth`" in out
    assert "user recorded `launch` on `a@demo`: local /x" in out and f"`{demo / 'AGENTS.md'}`" in out
    code, out, _ = edr(capsys, "brief", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and {"project", "root", "repo", "sources", "backend", "stages", "hosts", "tools", "batches",
                          "live", "decisions", "events", "read", "docs"} <= data.keys()
    assert data["hosts"]["hosts"][0]["start"] is False and [d["handle"] for d in data["decisions"]] == ["b@demo", "a@demo"]


def test_brief_names_the_sources_of_a_batch_and_how_far_each_lags_the_ref(demo: Path, capsys) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=demo, check=True, capture_output=True)
    repo = demo / "repo"
    old = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    for n in (1, 2):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                        "--allow-empty", "-m", f"c{n}"], check=True)
    new = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = f"{old}-dirty-0badc0de"
    for label, source in (("a", old), ("b", dirty), ("c", new), ("d", "deadbee")):
        seed(demo, label, "done", source=source)
    seed(demo, "e", "done", source=new, batch="next")
    code, out, _ = edr(capsys, "brief")
    lag = {old: "2 commits behind `HEAD`", dirty: "2 commits behind `HEAD`", new: "0 commits behind `HEAD`",
           "deadbee": "lag unknown"}
    assert code == 0 and ("Batch `demo` has 4 runs: 4 done. Its sources are " + board.join(
        [f"`{s}` ({lag[s]})" for s in sorted(lag)]) + ".") in out
    assert f"Batch `next` has 1 run: 1 done. Its source is `{new}` (0 commits behind `HEAD`)." in out
    data = json.loads(edr(capsys, "--json", "brief")[1])["data"]
    assert data["ref"] == "HEAD" and data["batches"][0]["sources"] == [
        {"source": s, "behind": {old: 2, dirty: 2, new: 0}.get(s)} for s in sorted(lag)]


def test_brief_proposes_nothing_for_a_retired_run(demo: Path, capsys) -> None:
    seed(demo, "b", "FAILED:synth", exit=5)
    seed(demo, "c", "stage:synth", pid=dead_pid(), updated=int(time.time()) - 3600)
    assert edr(capsys, "retire", "b@demo", "--uncollected", "--why", "failed")[0] == 0
    assert edr(capsys, "retire", "c@demo", "--uncollected", "--why", "gone")[0] == 0
    code, out, _ = edr(capsys, "brief", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["decisions"] == [] and data["batches"][0]["states"] == {"retired": 2}

def test_every_view_prints_a_value_the_same_way_under_the_project_name(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    add_metric(demo, a, "area_cell_um2", 123.4560000000001, step=5, stage="pnr")
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234")
    assert code == 0 and ["a", "abc1234", "pnr", "5", "area_cell_um2", "123.456", "u"] in [ln.split() for ln in out.splitlines()]
    brief = edr(capsys, "brief", "--run", "a@demo")[1]
    assert "- `area_cell_um2` is 123.456 u at `pnr` step 5." in brief
    detail = edr(capsys, "status", "a@demo")[1]
    assert ["pnr", "5", "area_cell_um2", "123.456", "u"] in [ln.split() for ln in detail.splitlines()]
    board_text = edr(capsys, "status", "--metric", "design__instance__area")[1]
    assert board_text.splitlines()[0].endswith(" area_cell_um2") and board_text.splitlines()[2].endswith(" 123.456 (pnr 5)")
    bot = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False))).metric_text("area_cell_um2", None)
    assert bot.splitlines()[1].endswith(" 123.456 u")
    assert all("design__instance__area" not in text for text in (out, brief, detail, board_text, bot))
    # --json and CSV keep the stored value and the canonical name.
    m = json.loads(edr(capsys, "--json", "metrics", "--source", "abc1234")[1])["data"][0]
    assert (m["name"], m["canonical"], m["value"]) == ("area_cell_um2", "design__instance__area", 123.4560000000001)
    assert ",area_cell_um2,design__instance__area,123.4560000000001,u," in edr(capsys, "metrics", "--source", "abc1234", "--csv")[1]


def test_brief_run_tells_a_failed_run_with_its_command(demo: Path, capsys) -> None:
    b = seed(demo, "b", "FAILED:synth", exit=5, stage="synth", step=2)
    log = Path(json.loads((bdir(demo) / f"{b}.json").read_text())["root"]) / "log" / "synth.log"
    log.write_text("".join(f"line {i}\n" for i in range(30)) + "Error: no licence\n")
    now = int(time.time())
    hb = json.loads((bdir(demo) / f"{b}.json").read_text())
    (bdir(demo) / f"{b}.json").write_text(json.dumps({
        **hb, "exit": 5, "log": str(log), "step_times": {"synth": {"1": now - 90, "2": now - 50}},
        "stages": {"synth": {"attempt": 1, "status": "failed", "started": now - 90, "ended": now - 10, "exit": 5},
                   "setup": {"attempt": 1, "status": "done", "started": now - 100, "ended": now - 95, "exit": 0}}}))
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("watch", b, "failed", "synth ended FAILED")
    add_metric(demo, b, "area_cell_um2", 12.5)
    code, out, _ = edr(capsys, "brief", "--run", "b@demo")
    assert code == 0 and out.startswith("# b@demo\n") and f"`{b}`" in out and "ended with the phase `FAILED:synth` and exit 5, so its state is failed." in out
    assert "- Stage `synth`, attempt 1," in out and "ran for 1m, ending failed with exit 5." in out
    assert "- The runtime setup started " in out and " and ran for 5s, ending done with exit 0.\n- Stage `synth`" in out
    assert "  - Step 2 (elaborate) started" in out and "watch recorded `failed` on `b@demo`: synth ended FAILED" in out
    assert "Error: no licence" in out and "line 11\n" in out and "line 10\n" not in out
    assert "`area_cell_um2` is 12.5 u at `synth` step 3." in out
    assert "The triage proposes `edr retire b@demo --why FAILED:synth`." in out and "the run ended `FAILED`" in out
    code, out, _ = edr(capsys, "status", "b@demo")
    rows = [ln.split()[0] for ln in out.splitlines() if ln.split()[:1] in (["setup"], ["synth"])]
    assert code == 0 and rows[:2] == ["setup", "synth"] and re.search(r"setup +1 +done +0 .* 0m", out)
    code, out, _ = edr(capsys, "--json", "brief", "--run", "b@demo")
    data = json.loads(out)["data"]
    assert code == 0 and data["command"] == "edr retire b@demo --why FAILED:synth" and data["state"] == "failed"
    assert {"runtime", "events", "log_tail", "metrics", "reason"} <= data.keys()
    code, _, err = edr(capsys, "brief", "--run", "nope@demo")
    assert code == 1 and "nope" in err


def test_brief_run_says_how_long_a_live_stage_has_run_so_far(demo: Path, capsys) -> None:
    a = seed(demo, "a", "stage:synth")
    now = int(time.time())
    hb = json.loads((bdir(demo) / f"{a}.json").read_text())
    (bdir(demo) / f"{a}.json").write_text(json.dumps({
        **hb, "step_times": {"synth": {"0": now - 600, "1": now - 300}},
        "stages": {"synth": {"attempt": 1, "status": "running", "started": now - 600}}}))
    out = edr(capsys, "brief", "--run", "a@demo")[1]
    assert "- Stage `synth`, attempt 1, started " in out and " and has run for 10m so far." in out
    assert "  - Step 0 (setup) started " in out and " and took 5m.\n  - Step 1 (analyze) started " in out
    assert out.count("has run for 5m so far.") == 1


# keep, stop, actions


def test_keep_writes_the_hours(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    assert edr(capsys, "keep", "b@demo")[0] == 1
    code, out, _ = edr(capsys, "keep", "b_nodw@demo", "--hours", "3", "--dry-run")
    assert code == 0 and "(dry)" in out and not list(bdir(demo).glob("*.keep.json"))
    assert edr(capsys, "keep", "b_nodw@demo", "--hours", "3")[0] == 0 and keep_file(demo, b) == {"hours": 3}
    assert edr(capsys, "keep", "b_nodw@demo")[0] == 0 and keep_file(demo, b) == {"hours": 12}
    assert edr(capsys, "keep", "b_nodw@demo", "--ack")[0] == 1  # no such flag
    code, out, _ = edr(capsys, "keep", "a@demo")
    assert code == 2 and "already done" in out
    with Database(demo / "data" / "edr.db") as db:
        assert [(e["actor"], e["kind"], e["text"]) for e in db.events()] == [("user", "keep", "keep 3 h"),
                                                                              ("user", "keep", "keep 12 h")]


def test_stop_after_task_now_and_finished(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    state = bdir(demo)
    assert edr(capsys, "stop", "b_nodw@demo")[0] == 1
    code, out, _ = edr(capsys, "stop", "a@demo", "--why", "t")
    assert code == 2 and "already done" in out
    code, out, _ = edr(capsys, "stop", "b_nodw@demo", "--after-task", "--why", "t", "--dry-run")
    assert code == 0 and not (state / f"{b}.stop").exists()
    code, out, _ = edr(capsys, "stop", "b_nodw@demo", "--after-task", "--why", "later")
    assert code == 0 and (state / f"{b}.stop").read_text().strip() == "after-task"
    code, out, _ = edr(capsys, "--json", "stop", "b_nodw@demo", "--now", "--why", "gone")
    assert code == 0 and json.loads(out)["data"]["stopped"] and "TERM driver" in json.loads(out)["output"]
    with Database(demo / "data" / "edr.db") as db:
        texts = [e["text"] for e in db.events(run_id=b)]
    assert len(texts) == 2 and texts[0] == "after-task: later" and texts[1].startswith("gone [driver")


def test_launch_exit_codes(demo: Path, capsys, monkeypatch) -> None:
    def row(started=False, queued=False, *problems):
        return {"run_id": "r", "label": "l", "host": "local", "root": "", "started": started, "queued": queued,
                "problems": list(problems), "pid": None}
    old, sync_failed = row(False, False, "already launched: x exists"), row(False, False, "sync failed")
    monkeypatch.setattr(cli.Ctx, "batch", lambda self, name, dry_run=False: name)
    cases = [([row(True), old], 0), ([row(False, True), old], 0), ([row(True), sync_failed], 0),
             ([old, old], 2), ([], 2), ([sync_failed, old], 1), ([sync_failed], 1)]
    for rows, code in cases:
        monkeypatch.setattr(launch, "launch", lambda *a, **k: rows)
        assert edr(capsys, "launch", "demo")[0] == code, rows
    monkeypatch.setattr(launch, "launch", lambda *a, **k: [row(), row()])
    assert edr(capsys, "launch", "demo", "--dry-run")[0] == 0
    monkeypatch.setattr(launch, "launch", lambda *a, **k: [row(), old])
    assert edr(capsys, "launch", "demo", "--dry-run")[0] == 2


def test_stop_waits_60_s_then_says_now(demo: Path, capsys, monkeypatch) -> None:
    b = seed(demo, "b_nodw", "stage:synth", pid=os.getpid())
    cmds: list[str] = []

    class AliveSsh(Ssh):
        def run(self, host, cmd, timeout_s=None):
            cmds.append(cmd)
            return 0, f"{os.getpid()}\n", ""

    class Clock:
        now, sleeps = 0.0, []

        def time(self):
            return self.now

        def sleep(self, s):
            self.sleeps.append(s)
            self.now += s

    clock = Clock()
    monkeypatch.setattr(cli, "Ssh", AliveSsh)
    monkeypatch.setattr(launch, "time", clock)
    code, out, _ = edr(capsys, "stop", "b_nodw@demo", "--why", "t")
    assert code == 3 and f"{b}: still alive; use --now" in out
    assert cmds[0] == f"kill -TERM {os.getpid()}" and clock.sleeps == [2] * 30
    assert cmds[1:] == [f"ps -p {os.getpid()} -o pid="] * 30
    cmds.clear()
    clock.sleeps.clear()
    code, out, _ = edr(capsys, "stop", "b_nodw@demo", "--now", "--why", "t")
    assert code == 3 and "use --now" not in out and clock.sleeps == [2] * 15
    assert cmds[0] == f"kill -TERM {os.getpid()}" and cmds[-2] == f"kill -KILL {os.getpid()}"
    with Database(demo / "data" / "edr.db") as db:
        assert [e["text"].endswith(", alive]") for e in db.events(run_id=b)] == [True, True]


def test_stop_marks_a_queued_run_stopped(demo: Path, capsys) -> None:
    q = seed(demo, "q", None, tree=False, state="queued")
    code, out, _ = edr(capsys, "stop", "q@demo", "--why", "t", "--dry-run")
    with Database(demo / "data" / "edr.db") as db:
        assert code == 0 and "(dry)" in out and db.run(q)["state"] == "queued"
    code, out, _ = edr(capsys, "--json", "stop", "q@demo", "--why", "t")
    assert code == 0 and json.loads(out)["data"] == {"run_id": q, "stopped": True}
    with Database(demo / "data" / "edr.db") as db:
        assert db.run(q)["state"] == "stopped" and db.runs(state="queued") == []
        assert [(e["kind"], e["text"]) for e in db.events(run_id=q)] == [("stop", "queued: t")]
    code, _, err = edr(capsys, "stop", "q@demo", "--why", "t")
    assert code == 1 and "no driver pid" in err


def test_actions_for_the_bot(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    add_metric(demo, a, "area_cell_um2", 1000.0)
    add_metric(demo, a, "area_cell_um2", 1031.5, step=5, stage="pnr")
    add_metric(demo, b, "area_cell_um2", 999.0)
    add_metric(demo, a, "power_w", 0.25, step=None, task="k_small")
    add_metric(demo, b, "power_w", 0.3, step=None, task="k_small")
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert acts.keep("b_nodw@demo", 5, "telegram") == ("b_nodw@demo: keep 5 h, and no automatic stop or kill for as long "
                                                         "unless its host runs out of scratch")
    assert keep_file(demo, b) == {"hours": 5} and acts.keep("a@demo", 5, "telegram") == "a@demo already done"
    assert acts.stop_after_task("b_nodw@demo", "telegram", "why") == "b_nodw@demo stops after its task"
    assert acts.stop_after_task("a@demo", "telegram", "why") == "a@demo already done"
    assert (bdir(demo) / f"{b}.stop").exists()
    with Database(demo / "data" / "edr.db") as db:
        assert {e["actor"] for e in db.events()} == {"telegram"} and len(db.events()) == 2
    assert acts.status_text().splitlines() == [
        "<b>Running</b>", "🟢 <code>b_nodw@demo</code> synth 2/4 elaborate, 1m", "", "<b>Finished in the last 24 hours</b>",
        "⚪ <code>a@demo</code> done, ended 0m ago", "", "<i>1 running, 1 done</i>", "<i>🟢 running, ⚪ done</i>"]
    with Database(demo / "data" / "edr.db") as db:
        assert len(db.get_store("last_board")) == 2
    one = acts.status_text("b_nodw@demo").splitlines()
    assert one == ["🟢 <code>b_nodw@demo</code> running", "stage synth, step 2 elaborate", "on local, 0m",
                   "<pre>step 2 elaborate</pre>"]
    assert acts.status_text("a@demo").splitlines()[3] .startswith("<code>edr export --source ")
    events = acts.events_text(2).splitlines()
    assert events[0][5:] == " <b>stop</b> <code>b_nodw@demo</code>" and events[1] == "    <i>after-task: why</i>"
    assert len(events) == 4 and "keep" in events[2]
    assert b not in acts.events_text(8)
    cmp = acts.compare_text(["a@demo", "b_nodw@demo"]).splitlines()
    # The area of a is at its step of record, pnr 5; b_nodw has no pnr step, so it is named missing. The percent is
    # against the first run.
    assert cmp[:3] == ["area_cell_um2 u", "  a       1031.5 (pnr 5)", "  b_nodw               -"]
    assert cmp[3:] == ["power_w[k_small] u", "  a       0.25", "  b_nodw   0.3  +20.0%", "missing: b_nodw has no pnr step"]
    page = acts.compare_page(["b_nodw@demo", "a@demo"]).decode()
    assert json.loads(re.search(r'id="edr-tick">(.*?)</script>', page)[1]) == {"runs": [b, a], "named": True}
    assert len(acts.metric_text("design__instance__area", None).splitlines()) == 4 and acts.metric_text("design__instance__area", "zzz") == "no metrics"
    assert acts.hosts_text().startswith("🟢 <b>local</b> free ")
    assert acts.tools_text() == "<b>demo</b> 2/10 seats used, local"
    assert acts.metrics_csv("abc1234").decode().splitlines()[0].startswith("run_id,label,")
    assert len(acts.metrics_csv("abc1234").decode().splitlines()) == 6 and acts.metrics_csv("zzz").count(b"\n") == 1
    assert acts.board_files() == []
    (demo / "data" / "board").mkdir(parents=True)
    (demo / "data" / "board" / "status.html").write_text("<html></html>")
    assert [p.name for p in acts.board_files()] == ["status.html"]
    with pytest.raises(Refuse, match="names no log"):
        acts.log_tail("a@demo", 5)
    info = acts.run_info("a@demo")
    assert info["handle"] == "a@demo" and info["run_id"] == a and info["host"] == "local" and info["run_root"].endswith(a)
    for text in ("\n".join(cmp), acts.metric_text("design__instance__area", None)):
        assert "\x1b" not in text and all(len(ln) <= 40 for ln in text.splitlines()), text
    assert acts.free_space("b_nodw@demo", "telegram") == f"{a}: rm -rf {info['run_root']}/out on local"
    assert not (Path(info["run_root"]) / "out").exists() and (Path(info["run_root"]) / "log").exists()
    capsys.readouterr()



def test_the_router_finds_the_project_and_the_run_of_a_press_or_a_command(demo: Path, tmp_path: Path, capsys,
                                                                            monkeypatch) -> None:
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    assert edr(capsys, "register")[0] == 0
    beta = tmp_path / "edr" / "beta"
    shutil.copytree(demo, beta, ignore=shutil.ignore_patterns("data"))
    (beta / "edr.toml").write_text((beta / "edr.toml").read_text().replace('project = "demo"', 'project = "beta"'))
    monkeypatch.chdir(beta)
    seed(beta, "b_nodw", "stage:synth", pid=dead_pid(), date="20260926_1300")
    assert edr(capsys, "register")[0] == 0
    monkeypatch.chdir(tmp_path)
    token = tmp_path / "token"
    token.write_text("1:A")
    router = cli.Router(argparse.Namespace(json=False, dry_run=False, command="serve"))
    assert router.names() == ["beta", "demo"] and router.resolve("demo/b_nodw@demo") == ("demo", "b_nodw@demo")
    with pytest.raises(Refuse, match="more than one project has it: beta/b_nodw@demo, demo/b_nodw@demo"):
        router.resolve("b_nodw@demo")
    assert router.pick(["beta", "x"], None) == ("beta", ["x"]) and router.pick(["x"], "demo") == ("demo", ["x"])
    with pytest.raises(Refuse, match="name a project first: beta, demo"):
        router.pick(["x"], None)
    project = config.load_project(demo)
    tg = Telegram(token_file=token, chat_id=42)
    with Database(project.data / "edr.db") as db:
        sender = TelegramBot(tg, project.site, project, db, None, str(token))
        sender.api.call = lambda method, params, files=None: {"message_id": 7}
        alert = watch.alerts.run_alert(project, db.run(b), "hung", ["hung: x"], json.loads((bdir(demo) / f"{b}.json").read_text()),
                                       time.time())
        sender.send(alert)  # the watcher of demo remembers the run of message 7
    assert router.run_of("demo", 7) == b and router.replied(7) == ("demo", b) and router.run_of("beta", 7) is None
    bot = TelegramBot(tg, project.site, project, home.Store(), router, str(token))
    bot.api.call = lambda method, params, files=None: {}
    press = {"id": "q", "from": {"id": 7}, "data": "keep6:demo", "message": {"message_id": 7, "chat": {"id": 42}, "text": "x"}}
    bot.handle_update({"update_id": 1, "callback_query": press})
    assert keep_file(demo, b) == {"hours": 6} and not list((beta / "data").glob("x"))
    with Database(project.data / "edr.db") as db:
        assert [(e["actor"], e["run_id"], e["kind"]) for e in db.events()] == [("telegram", b, "keep")]
    assert "beta" in router.projects_text() and "demo" in router.board_text()
    assert "b_nodw" in router.events_text(5) and router.tools_text().startswith("<b>demo</b>")
    hosts = router.hosts_text().splitlines()  # max_per_host counts the runs of both projects
    assert hosts[0].startswith("🔴 <b>local</b>") and hosts[0].endswith("yours: beta 1, demo 1, 0 cores, 0 GB scratch")
    assert hosts[1] == "    <i>2 of your runs, max_per_host is 2</i>" and "<b>beta</b>" in router.digest_text()


# retire


def test_retire_guards_prune_abandon_and_batch(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    q = seed(demo, "q", None, tree=False, state="queued")
    c = seed(demo, "c", "stage:synth", pid=os.getpid(), batch="other")
    roots = {r: Path(config.load_project(demo).site.scratch[0]) / getpass.getuser() / "edr" / "demo" / r for r in (a, b, c)}
    state = bdir(demo).parent
    assert edr(capsys, "retire", "--why", "x")[0] == 1
    assert edr(capsys, "retire", "a@demo")[0] == 1
    code, _, err = edr(capsys, "retire", "a@demo", "--why", "x")
    assert code == 1 and "not collected" in err and roots[a].is_dir()
    code, _, err = edr(capsys, "retire", "a@demo", "--prune", "nope", "--why", "x")
    assert code == 1 and "prune.nope" in err
    code, out, _ = edr(capsys, "retire", "a@demo", "--prune", "netlist", "--why", "x", "--dry-run")
    assert code == 0 and f"rm -rf {roots[a]}/out on local (dry)" in out and (roots[a] / "out" / "11").is_dir()
    code, out, _ = edr(capsys, "retire", "a@demo", "--prune", "netlist", "--why", "x")
    assert code == 0 and not (roots[a] / "out").exists() and (roots[a] / "log").is_dir()
    (demo / "data" / "results" / a / "log").mkdir(parents=True)
    code, out, _ = edr(capsys, "retire", "a@demo", "--why", "x")
    assert code == 0 and not roots[a].exists()
    assert json.loads((state / "demo" / f"{a}.json").read_text())["phase"] == "done"
    code, _, err = edr(capsys, "retire", "c@other", "--uncollected", "--why", "x")
    assert code == 1 and "is alive" in err and roots[c].is_dir()
    (roots[b] / "log").mkdir(exist_ok=True)
    code, out, err = edr(capsys, "retire", "--batch", "demo", "--why", "gone", "--dry-run")
    assert code == 1 and "not collected" in err and "rm -rf" not in out  # a dry run still runs the guard
    code, _, err = edr(capsys, "retire", "--batch", "demo", "--why", "gone")
    assert code == 1 and "not collected" in err and roots[b].is_dir()  # every guard runs before the first rm
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "gone", "--dry-run")
    assert code == 0 and roots[b].is_dir() and not (state / "demo" / "RETIRED").exists() and "(dry)" in out
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "gone")
    assert code == 0 and not roots[b].exists() and (state / "demo" / "RETIRED").exists()
    hb = json.loads((state / "demo" / f"{b}.json").read_text())
    assert hb["phase"] == "ABANDONED:gone" and hb["exit"] == 1
    with Database(demo / "data" / "edr.db") as db:
        assert db.batches()[0]["retired"] and db.run(b)["phase"] == "ABANDONED:gone"
        assert db.run(q)["state"] == "retired" and db.run(a)["phase"] == "done"
        kinds = [(e["run_id"], e["kind"]) for e in db.events()]
    assert kinds == [(a, "prune"), (a, "retire"), (a, "retire"), (b, "retire"), (q, "retire")]
    assert edr(capsys, "status", "--batch", "demo")[1] == "no runs\n" and "c" in edr(capsys, "status")[1]
    assert edr(capsys, "retire", "--batch", "empty", "--why", "x")[0] == 2


def test_a_retired_live_run_stays_retired_and_the_watcher_does_not_resume_it(demo: Path, capsys, monkeypatch) -> None:
    b = seed(demo, "b", "stage:synth", pid=dead_pid(), updated=int(time.time()) - 3600)
    code, _, _ = edr(capsys, "retire", "b@demo", "--uncollected", "--why", "gone")
    assert code == 0
    resumed, sent = [], []
    monkeypatch.setattr(watch, "_resume", lambda *a, **k: resumed.append(a))
    notifier = Notifier()
    monkeypatch.setattr(notifier, "send", lambda *a, **k: sent.append(a))
    project = config.load_project(demo)
    with Database(project.data / "edr.db") as db:
        assert watch.cycle(project, Ssh(project.site), db, [notifier])[b] == "retired"
        assert db.run(b)["phase"] == "ABANDONED:gone" and not board.is_live(db.run(b))
    assert resumed == [] and sent == []


# metrics, export, hosts, tools


def test_metrics_and_export(demo: Path, capsys, tmp_path: Path) -> None:
    a = seed(demo, "a", "done")
    add_metric(demo, a, "area_cell_um2", 1000.0)
    add_metric(demo, a, "wns_ns", -0.5, step=3)
    (demo / "data" / "results" / a / "reports" / "3").mkdir(parents=True)
    (demo / "data" / "results" / a / "reports" / "3" / "area.rpt").write_text("i_top 1000.0\n")
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234", "--csv")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("run_id,label,config,build_tag,source,host,stage,step,task,metric") and len(lines) == 3
    assert lines[1].startswith(f"{a},a,demo,,abc1234,local,synth,3,,area_cell_um2,design__instance__area,1000.0,u,")
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234", "--stage", "pnr")
    assert code == 2 and out == "no metrics\n"
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234", "--step", "3")
    assert code == 0 and "wns_ns" in out
    assert edr(capsys, "metrics", "--source", "abc")[0] == 2  # exact match, not a prefix
    exp = demo / "exports" / "abc1234"
    code, out, _ = edr(capsys, "export", "--source", "abc1234", "--out", "exports/abc1234", "--dry-run")
    assert code == 0 and not exp.exists() and "metrics.csv" in out and "area.rpt" not in out
    code, out, _ = edr(capsys, "--json", "export", "--source", "abc1234", "--out", "exports/abc1234", "--files", "--with", "reports/*/*.rpt")
    manifest = json.loads(out)["data"]
    assert code == 0 and manifest == json.loads((exp / "manifest.json").read_text()) and (exp / a / "reports" / "3" / "area.rpt").exists()
    code, _, err = edr(capsys, "export", "--source", "abc1234", "--out", str(exp))
    assert code == 1 and "not empty" in err
    with Database(demo / "data" / "edr.db") as db:
        assert [(e["kind"], e["text"]) for e in db.events()] == [("export", f"abc1234 -> {exp}")]


def test_two_runs_of_a_label_in_one_batch_are_named_apart(demo: Path, capsys, tmp_path: Path) -> None:
    dirty_tag = "abc1234-dirty-0badc0de"
    clean, dirty = seed(demo, "a", "FAILED:synth", exit=5), seed(demo, "a", "done", source=dirty_tag)
    bare = seed(demo, "i", "FAILED:pnr", tree=False)  # no tree: a retire would only mark its row
    for run in (clean, dirty):
        add_metric(demo, run, "area_cell_um2", 1000.0)
    code, _, err = edr(capsys, "status", "a@demo")
    assert code == 1 and f"{clean} (abc1234, FAILED:synth)" in err and f"{dirty} ({dirty_tag}, done)" in err
    # The clean id starts the dirty one, so only the whole clean id names the clean run alone.
    code, out, _ = edr(capsys, "status", "--triage")
    assert code == 0 and f"edr retire {clean} --why FAILED:synth" in out and "i@demo" not in out
    decisions = json.loads(edr(capsys, "--json", "brief")[1])["data"]["decisions"]
    assert [(d["handle"], d["command"]) for d in decisions] == [
        (clean, f"edr retire {clean} --why FAILED:synth"), (clean + "-", f"edr export --source {dirty_tag} --out exports/{dirty_tag}")]
    assert edr(capsys, "retire", clean, "--uncollected", "--why", "x", "--dry-run")[2] == f"{clean}: phase FAILED:synth\n"
    assert edr(capsys, "stop", f"a@{dirty_tag}", "--why", "x", "--dry-run")[1:] == (f"{dirty}: already done\n",
                                                                                    f"{dirty}: phase done\n")
    code, out, err = edr(capsys, "continue", clean, "--stage", "synth", "--dry-run")
    assert code == 0 and err == f"{clean}: phase FAILED:synth\n" and "a.synth" in out
    assert len(edr(capsys, "metrics", "--source", "abc1234", "--source", dirty_tag, "--csv")[1].splitlines()) == 3
    out = edr(capsys, "extract", "--source", "abc1234", "--source", dirty_tag, "--dry-run")[1]
    assert {ln.split(": ")[0] for ln in out.splitlines()} == {clean, dirty, bare}
    exp = tmp_path / "exp"
    code, out, _ = edr(capsys, "export", "--source", "abc1234", "--source", dirty_tag, "--labels", "a", "--out", str(exp))
    manifest = json.loads((exp / "manifest.json").read_text())
    assert code == 0 and out == f"{exp}: 2 runs (1 not done), 0 skipped, 6 files\n"
    assert [r["run_id"] for r in manifest["runs"]] == [clean, dirty] and manifest["sources"] == ["abc1234", dirty_tag]


def test_extract_replaces_changed_rows(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    add_metric(demo, a, "area_cell_um2", 1000.0)  # unit u, the config says um2
    reports = demo / "data" / "results" / a / "reports" / "3"
    reports.mkdir(parents=True)
    (reports / "area.rpt").write_text("i_top 1000.0\n")
    (reports / "qor.rpt").write_text(demo_qor(3))
    assert edr(capsys, "extract")[0] == 1 and edr(capsys, "extract", "a@demo", "--batch", "demo")[0] == 1
    code, out, _ = edr(capsys, "extract", "a@demo", "--dry-run")
    assert code == 0 and out == f"{a}: 2 new, 1 changed, 0 unchanged, 0 failed, 0 removed (dry)\n"
    with Database(demo / "data" / "edr.db") as db:
        assert [m["unit"] for m in db.metrics(run_ids=[a])] == ["u"] and db.events() == []
    code, out, _ = edr(capsys, "--json", "extract", "--source", "abc1234")
    assert code == 0 and json.loads(out)["data"] == [{"run_id": a, "new": 2, "changed": 1, "unchanged": 0, "failed": 0,
                                                      "removed": 0, "kept": 0, "failures": {}, "flags": [], "clashes": []}]
    with Database(demo / "data" / "edr.db") as db:
        assert {(m["name"], m["value"], m["unit"]) for m in db.metrics(run_ids=[a])} == {
            ("area_cell_um2", 1000.0, "um2"), ("wns_ns", -0.03, "ns"), ("setup_violations", 3.0, "paths")}
        assert [(e["kind"], e["text"]) for e in db.events()] == [("extract", "2 new, 1 changed, 0 unchanged, 0 failed, 0 removed")]
    code, out, _ = edr(capsys, "extract", "--batch", "demo")
    assert code == 0 and out == f"{a}: 0 new, 0 changed, 3 unchanged, 0 failed, 0 removed\n"
    assert edr(capsys, "extract", "--source", "0000000")[0] == 2


def test_extract_takes_every_step_of_a_stage_over_budget_with_exit_0(demo: Path, capsys) -> None:
    a = seed(demo, "a", "OVER_BUDGET:pnr")
    hb_path = bdir(demo) / f"{a}.json"
    hb = json.loads(hb_path.read_text())
    hb.update(stage="pnr", step=5, exit=9, stages={"synth": {"status": "done", "exit": 0},
                                                   "pnr": {"status": "over_budget", "exit": 0}})
    hb_path.write_text(json.dumps(hb))
    with Database(demo / "data" / "edr.db") as db:
        db.set_step_times(a, {"synth": {str(n): n for n in range(4)}, "pnr": {"4": 4, "5": 5}})
    for n in range(6):
        reports = demo / "data" / "results" / a / "reports" / str(n)
        reports.mkdir(parents=True)
        (reports / "area.rpt").write_text(f"i_top {1000 + n}\n")
        (reports / "qor.rpt").write_text(demo_qor(n))
    code, out, _ = edr(capsys, "extract", "a@demo")
    assert code == 0 and out == f"{a}: 18 new, 0 changed, 0 unchanged, 0 failed, 0 removed\n"
    with Database(demo / "data" / "edr.db") as db:
        steps = {(m["stage"], m["step"]) for m in db.metrics(run_ids=[a])}
    assert steps == {("synth", n) for n in range(4)} | {("pnr", 4), ("pnr", 5)}


def test_extract_rebuilds_the_rows_of_a_run_from_its_files(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    beat(demo, a, tasks={"power": {"k_small": {"phase": "done"}, "k_big": {"phase": "done"}}})
    results = demo / "data" / "results" / a
    for n in (2, 3, 4):
        (results / "reports" / str(n)).mkdir(parents=True)
        (results / "reports" / str(n) / "area.rpt").write_text(f"i_top {1000 + n}\n")
        (results / "reports" / str(n) / "qor.rpt").write_text(demo_qor(n))
    for test, whole in (("GEMM_M64_N64", "WHOLE,0.250\n"), ("SOFTMAX_N512", "")):
        power = results / "simulation" / "tests" / "demo" / test / "power"
        (power / "reports").mkdir(parents=True)
        (power / "reports" / "power.csv").write_text("phase,total_w\n" + whole)
        (power / "phases.json").write_text('{"window_ns": 3400}')
    with Database(demo / "data" / "edr.db") as db:
        for stage, step, task, name, value, unit, canonical, source in (
                ("power", None, "k_small", "energy_nj", 850.0, "nJ", "energy", "power_w * window_ns"),  # an old parser
                ("pnr", 3, "", "area_cell_um2", 1003.0, "um2", "design__instance__area", "reports/3/area.rpt:1"),
                ("synth", 1, "", "area_cell_um2", 1001.0, "um2", "design__instance__area", "reports/1/area.rpt:1"),
                ("pnr", 4, "", "area_cell_um2", 1004.0, "um2", "design__instance__area", "reports/4/area.rpt:1"),
                ("pnr", 5, "", "wns_ns", None, "ns", "timing__setup__ws", "reports/5/qor.rpt: no match"),
                ("power", None, "k_gone", "window_ns", 3400.0, "ns", "", "simulation/tests/demo/GEMM_M64_N64/power/phases.json")):
            db.add_metric({"run_id": a, "stage": stage, "step": step, "task": task, "name": name, "value": value,
                           "unit": unit, "canonical": canonical, "source_file": source})
    failures = ("  power_w: 1 failed: simulation/tests/demo/SOFTMAX_N512/power/reports/power.csv: no row matches "
                "{'phase': 'WHOLE'}\n  energy_nj: 1 failed: simulation/tests/demo/SOFTMAX_N512/power/phases.json: "
                "power.csv has no WHOLE row\n")
    # pnr owns steps 4 and 5, so its row at step 3 goes; the file of step 1 is gone, so its row stays. The run has
    # passed no pnr step and k_gone is no task, so the extraction reads neither: their values stay, a failure goes.
    code, out, _ = edr(capsys, "extract", "a@demo", "--dry-run")
    assert code == 0 and out == f"{a}: 9 new, 1 changed, 0 unchanged, 2 failed, 2 removed, 1 kept without a file (dry)\n" + failures
    code, out, _ = edr(capsys, "--json", "extract", "a@demo")
    assert json.loads(out)["data"][0]["failures"]["power_w"] == {
        "count": 1, "first": "simulation/tests/demo/SOFTMAX_N512/power/reports/power.csv: no row matches {'phase': 'WHOLE'}"}
    with Database(demo / "data" / "edr.db") as db:
        rows = {(m["stage"], m["step"], m["task"], m["name"]): (m["value"], m["source_file"]) for m in db.metrics()}
        assert db.events()[-1]["text"] == ("9 new, 1 changed, 0 unchanged, 2 failed, 2 removed, 1 kept without a file; "
                                           + failures.strip().replace("\n  ", "; "))
    assert rows[("power", None, "k_small", "energy_nj")] == (850.0, "simulation/tests/demo/GEMM_M64_N64/power/phases.json")
    assert rows[("power", None, "k_big", "power_w")][0] is None and ("pnr", 3, "", "area_cell_um2") not in rows
    assert rows[("synth", 1, "", "area_cell_um2")] == (1001.0, "reports/1/area.rpt:1") and len(rows) == 15
    assert rows[("pnr", 4, "", "area_cell_um2")][0] == 1004.0 and rows[("power", None, "k_gone", "window_ns")][0] == 3400.0
    out = edr(capsys, "metrics", "--source", "abc1234")[1]
    assert "failed: simulation/tests/demo/SOFTMAX_N512/power/phases.json: power.csv has no WHOLE row" in out
    toml = demo / "edr.toml"
    text = toml.read_text()
    toml.write_text(text[:text.index("# A slack can print")] + text[text.index("[metrics.power_w]"):])
    code, out, _ = edr(capsys, "extract", "a@demo")
    assert out == f"{a}: 0 new, 0 changed, 8 unchanged, 2 failed, 2 removed, 1 kept without a file\n" + failures
    with Database(demo / "data" / "edr.db") as db:
        assert not db.metrics(name="setup_violations") and len(db.metrics()) == 13


CHECKS = """
[parameters.knobs]
stage = "synth"
file = "log/synth.log"
regex = 'set (?P<key>\\w+) (?P<value>[^;]+);'
head_bytes = 64

[parameters.source]
stage = "synth"
file = "reports/build.rpt"
regex = '^commit:\\s*(\\S+)'

[checks]
same_results = ["window_ns", "power_w"]
python = "hooks/checks.py:check"
"""


def test_extract_reads_parameters_from_the_files_and_flags_runs_that_contradict_their_identity(demo: Path, capsys) -> None:
    (demo / "edr.toml").write_text((demo / "edr.toml").read_text() + CHECKS)
    (demo / "hooks" / "checks.py").write_text(
        "def check(run, parameters, rows):\n"
        "    return [(m['task'], 'capped_window', 'the window is the cap of 4096 ns') for m in rows\n"
        "            if m['name'] == 'window_ns' and m['value'] == 4096]\n")
    a, b, c = (seed(demo, label, "done") for label in ("a", "b", "c"))
    d = seed(demo, "a.power", "done", tree_id=a)  # it continues a on the tree of a
    beat(demo, c, tasks={"power": {"k_small": {"phase": "done"}, "k_big": {"phase": "done"}}})
    for run, lanes, design in ((a, 8, "abc1234"), (b, 8, "abc1234"), (c, 4, "abc1234-dirty"), (d, 8, "abc1234")):
        results = demo / "data" / "results" / run
        (results / "log").mkdir(parents=True)
        # head_bytes stops the read in the dots, before the line that sets EXTRA.
        (results / "log" / "synth.log").write_text(f"step 0 setup\nset ENABLE_X 1; set LANES {lanes};\n{'.' * 40}\nset EXTRA 1;\n")
        (results / "reports").mkdir()
        (results / "reports" / "build.rpt").write_text(f"built at 12:00\ncommit: {design}\n")
    for test in ("GEMM_M64_N64", "SOFTMAX_N512"):  # k_small and k_big ran one test under two names
        power = demo / "data" / "results" / c / "simulation" / "tests" / "demo" / test / "power"
        (power / "reports").mkdir(parents=True)
        (power / "reports" / "power.csv").write_text("phase,total_w\nWHOLE,0.25\n")
        (power / "phases.json").write_text('{"window_ns": 4096}')
    with Database(demo / "data" / "edr.db") as db:
        for run in (a, b, c, d):
            db.set_parameters(run, {"source": "abc1234"}, "checkout")
        db.set_parameters(a, {"ENABLE_X": "0"}, "spec")  # the job override says 0
        db.set_parameters(b, {"LANES": "8.0"}, "spec")  # the same number as the log's 8

    code, out, _ = edr(capsys, "extract", "--batch", "demo")
    assert code == 0 and out.split(f"{c}: ")[1] == (
        "6 new, 0 changed, 0 unchanged, 0 failed, 0 removed, 3 parameters, 5 flags\n"
        "  flag declared_vs_observed: source is abc1234-dirty in the run's files and abc1234 by checkout\n"
        "  flag capped_window k_big: the window is the cap of 4096 ns\n"
        "  flag same_results k_big: window_ns and power_w equal those of k_small\n"
        "  flag capped_window k_small: the window is the cap of 4096 ns\n"
        "  flag same_results k_small: window_ns and power_w equal those of k_big\n")
    with Database(demo / "data" / "edr.db") as db:
        flags = {(f["run_id"], f["task"], f["check"], f["text"]) for f in db.flags() if f["run_id"] != c}
        extracted = {(p["run_id"], p["key"]): p["value"] for p in db.parameters() if p["origin"] == "extract"}
    # d shares the tree of a, so only b pairs with each of them.
    assert flags == {(a, "", "declared_vs_observed", "ENABLE_X is 1 in the run's files and 0 by spec"),
                     (a, "", "same_parameters", "the same extracted parameters as b@demo"),
                     (b, "", "same_parameters", "the same extracted parameters as a.power@demo and a@demo"),
                     (d, "", "same_parameters", "the same extracted parameters as b@demo")}
    assert extracted[(a, "LANES")] == "8" and extracted[(c, "source")] == "abc1234-dirty" and (a, "EXTRA") not in extracted

    out = edr(capsys, "status", "a@demo")[1]
    assert "flags" in out and "ENABLE_X is 1 in the run's files and 0 by spec" in out
    out = edr(capsys, "brief")[1]
    assert ("- `a@demo`: 1 `declared_vs_observed` and 1 `same_parameters`\n- `b@demo`: 1 `same_parameters`\n"
            "- `c@demo`: 1 `declared_vs_observed`, 2 `capped_window` and 2 `same_results`\n") in out
    out = edr(capsys, "brief", "--run", "c@demo")[1]
    assert ("## Flags\n\nThe checks flag this run:\n\n- `declared_vs_observed`: source is abc1234-dirty in the run's "
            "files and abc1234 by checkout\n- `capped_window` on task `k_big`: the window is the cap of 4096 ns\n") in out

    # Each extraction rewrites the flags: a now runs as declared, and b keeps d as its only twin.
    (demo / "data" / "results" / a / "log" / "synth.log").write_text("set ENABLE_X 0; set LANES 8;\n")
    assert edr(capsys, "extract", "a@demo")[1] == f"{a}: 0 new, 0 changed, 0 unchanged, 0 failed, 0 removed, 3 parameters\n"
    with Database(demo / "data" / "edr.db") as db:
        assert [(f["run_id"], f["text"]) for f in db.flags([a, b, d])] == [
            (d, "the same extracted parameters as b@demo"), (b, "the same extracted parameters as a.power@demo")]
    # A hook that fails is a flag of the run.
    (demo / "hooks" / "checks.py").write_text("def check(run, parameters, rows):\n    return 1 / 0\n")
    assert "  flag checks.python: hooks/checks.py:check: division by zero\n" in edr(capsys, "extract", "c@demo")[1]


def test_extract_writes_the_rows_of_a_run_in_one_commit(demo: Path, capsys, monkeypatch) -> None:
    a = seed(demo, "a", "done")
    (demo / "data" / "results" / a).mkdir(parents=True)
    (demo / "data" / "results" / a / "n.txt").write_text("n 7\n")
    toml = demo / "edr.toml"
    toml.write_text(toml.read_text() + "".join(f"\n[metrics.n{i}]\nstage = \"synth\"\nfile = \"n.txt\"\nregex = 'n (\\d+)'\n"
                                               for i in range(1000)))
    commits, connect = [], sqlite3.connect

    def traced(*args, **kwargs) -> sqlite3.Connection:
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    code, out, _ = edr(capsys, "extract", "a@demo")
    assert code == 0 and out == f"{a}: 1000 new, 0 changed, 0 unchanged, 0 failed, 0 removed\n" and commits == ["COMMIT"]


def test_a_read_command_opens_the_database_read_only_and_creates_no_file(demo: Path, capsys, tmp_path: Path) -> None:
    a = seed(demo, "a", "done")
    add_metric(demo, a, "area_cell_um2", 1000.0)
    data, demand = demo / "data", tmp_path / "demand.csv"
    demand.write_text("label,stage,task\na,synth,\n")
    reads = (("metrics", "--source", "abc1234"), ("compare", a), ("runtime", a), ("events",), ("coverage", str(demand)))
    for argv in reads:
        assert edr(capsys, *argv)[0] in (0, 2)
    assert sorted(p.name for p in data.iterdir()) == ["edr.db"]
    data.chmod(0o555)
    try:
        for argv in reads:
            assert edr(capsys, *argv)[0] in (0, 2)
        code, out, _ = edr(capsys, "metrics", "--source", "abc1234")
        assert code == 0 and "1000" in out and sorted(p.name for p in data.iterdir()) == ["edr.db"]
    finally:
        data.chmod(0o755)
    with Database(data / "edr.db") as db:  # a writer with the database open keeps its rows in the -wal file
        add_metric(demo, a, "wns_ns", -0.25)
        assert (data / "edr.db-wal").is_file() and "-0.25" in edr(capsys, "metrics", "--source", "abc1234")[1]
        assert db.metrics(name="wns_ns")


def test_hosts_and_tools_probe_local(demo: Path, capsys, tmp_path: Path) -> None:
    code, out, _ = edr(capsys, "hosts")
    row = out.splitlines()[2]
    assert code == 0 and "free cores" in out.splitlines()[0] and row.split()[:2] == ["🟢", "local"]
    code, out, _ = edr(capsys, "--json", "hosts", "--narrow")
    data = json.loads(out)["data"][0]
    assert code == 0 and data["host"] == "local" and data["runs"] == {} and data["mount"] == str(tmp_path / "scratch")
    beat(demo, seed(demo, "c", "stage:synth"), cpu_pct=250.0, tree_gb=3.5)
    data = json.loads(edr(capsys, "--json", "hosts")[1])["data"][0]
    assert (data["runs"], data["our_cores"], data["our_gb"]) == ({"demo": 1}, 2.5, 3.5)
    code, out, _ = edr(capsys, "tools")
    assert code == 0 and out.splitlines()[0].split() == ["tool", "free", "total", "hosts", "note"]
    assert out.splitlines()[2].split() == ["demo", "8", "10", "local", "1.0"]
    code, out, _ = edr(capsys, "--json", "tools")
    assert code == 0 and json.loads(out)["data"] == [{"tool": "demo", "free": 8, "total": 10, "hosts": {"local": "1.0"}}]
    site = demo / "site.toml"
    text = site.read_text()
    site.write_text(text.replace('["bash", "{root}/flow/seats.sh"]', '["false"]'))
    code, out, _ = edr(capsys, "tools")
    assert code == 3 and "unknown" in out
    site.write_text(text.replace('["bash", "{root}/flow/seats.sh"]', '["echo", "3"]') + '\n[tools.plain]\n')
    code, out, _ = edr(capsys, "--json", "tools")
    assert code == 0 and json.loads(out)["data"] == [{"tool": "demo", "free": 3, "total": 10, "hosts": {"local": "1.0"}},
                                                     {"tool": "plain", "hosts": {}}]  # local lists demo only
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert acts.tools_text().splitlines() == ["<b>demo</b> 7/10 seats used, local", "<b>plain</b>"]


# run (reuse), watch, bad input


def test_hosts_show_the_room_and_your_runs_where_a_run_can_start_first(demo: Path, capsys, monkeypatch,
                                                                         tmp_path: Path) -> None:
    site = demo / "site.toml"
    site.write_text(site.read_text().replace("host_free_min_gb = 1", "host_free_min_gb = 100")
                    + "\n[hosts.hostA]\ncores = 64\nram_gb = 256\n\n[hosts.hostB]\ncores = 32\nram_gb = 128\n")
    probes = {
        "local": HostProbe("local", 7.8, 35.0, "/tmp/x", 150.5, 3, 0, cores=8, load=0.2, total_ram_gb=62.3, total_gb=200.0),
        "hostA": HostProbe("hostA", 12.5, 120.0, "/scratch", 80.0, 4, 2, cores=64, load=51.5, total_ram_gb=256.0,
                           total_gb=2000.0, gpus=4, gpus_idle=1, gpu_used_gb=30.0, gpu_total_gb=320.0),
    }

    def probe(self, host):
        if host in probes:
            return probes[host]
        raise HostError(f"{host}: rc 255: timeout")

    monkeypatch.setattr(Ssh, "probe", probe)
    beat(demo, seed(demo, "a", "stage:synth"), host="hostA", tree_gb=1900.0, cpu_pct=100.0)
    code, out, _ = edr(capsys, "hosts")
    lines = out.splitlines()
    assert code == 3 and "\x1b" not in out
    assert [ln.split()[:2] for ln in lines[2:5]] == [["🟢", "local"], ["🔴", "hostA"], ["⚫", "hostB"]]
    assert next(ln for ln in lines if " hostA " in ln).split()[2:] == ["12.5/64", "120/256", "80/2000", "1/4", "4/2",
                                                                     "demo", "1", "1", "0", "1900"]
    assert lines[5:] == ["hostA: 80 GB scratch free, under the floor of 100 GB",
                         "hostA: your trees hold 1900 GB and push its scratch under the floor of 100 GB",
                         "hostB: hostB: rc 255: timeout"]
    code, out, _ = edr(capsys, "hosts", "--narrow")
    lines = out.splitlines()
    assert code == 3 and all(len(ln) <= 48 for ln in lines)
    assert next(ln for ln in lines if " hostA " in ln).split() == ["🔴", "hostA", "12.5/64", "80/2000", "1"]
    data = json.loads(edr(capsys, "--json", "hosts")[1])["data"]
    assert [(r["host"], r["start"]) for r in data] == [("local", True), ("hostA", False), ("hostB", None)]
    assert data[1]["floor_gb"] == 100 and data[1]["runs"] == {"demo": 1} and "error" in data[2]
    text = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False))).hosts_text().splitlines()
    assert text[:4] == ["🟢 <b>local</b> free 7.8/8 cores, 35/62 GB RAM, 150/200 GB scratch; none of yours",
                        "🔴 <b>hostA</b> free 12.5/64 cores, 120/256 GB RAM, 80/2000 GB scratch, 1/4 GPUs; yours: demo 1, "
                        "1 cores, 1900 GB scratch",
                        "    <i>80 GB scratch free, under the floor of 100 GB</i>",
                        "    <i>your trees hold 1900 GB and push its scratch under the floor of 100 GB</i>"]
    assert "⚫ <b>hostB</b> <i>no answer</i>" in text
    monkeypatch.chdir(tmp_path)  # outside a project: the default site file
    (tmp_path / ".config" / "edarunner").mkdir(parents=True)
    shutil.copy(site, tmp_path / ".config" / "edarunner" / "site.toml")
    data = json.loads(edr(capsys, "--json", "hosts")[1])["data"]
    # The default [placement] wants 16 free cores, so no run can start, and the most free cores go first.
    assert [(r["host"], r["start"], r["runs"]) for r in data] == [("hostA", False, {}), ("local", False, {}), ("hostB", None, {})]


def test_continue_reuse_dry_and_collect(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    with_vars(demo, a)
    assert edr(capsys, "continue", "a@demo")[0] == 2  # a spec without stages leaves none
    assert edr(capsys, "continue", "a@demo", "--stage", "nope")[0] == 1
    code, _, err = edr(capsys, "continue", "a@demo", "--stage", "export", "--on", "local", "--from", "cts", "--dry-run")
    assert code == 1 and "no resume" in err  # export has no resume command
    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--stage", "pnr", "--on", "local", "--from", "cts", "--dry-run")
    data = json.loads(out)["data"]
    assert code == 0 and data["batch"] == "demo" and data["host"] == "local"
    assert re.fullmatch(r"\d{8}_\d{4}_a\.pnr_demo_gabc1234", data["run_id"])
    spec = data["spec"]
    assert spec["start_at"] == {"stage": "pnr", "checkpoint": "cts"} and [s["name"] for s in spec["stages"]] == ["pnr"]
    assert spec["root"] == spec["stages"][0]["cwd"] and f"pnr {data['run_id']} demo" in spec["stages"][0]["cmd"]
    assert data["run_id"] != a and not (bdir(demo) / f"{data['run_id']}.spec.json").exists()
    code, _, err = edr(capsys, "continue", "a@demo", "--stage", "export", "--on", "mars", "--dry-run")
    assert code == 1
    code, out, _ = edr(capsys, "continue", "a@demo", "--collect", "netlist")
    assert code == 0 and "1 files" in out and (demo / "data" / "results" / a / "out" / "11").is_dir()
    code, out, _ = edr(capsys, "continue", "a@demo", "--collect", "nope")
    assert code == 3
    with Database(demo / "data" / "edr.db") as db:
        assert [e["kind"] for e in db.events()] == ["collect", "collect"]


def stages_of(out: str) -> tuple[str, list[str]]:
    data = json.loads(out)["data"]
    return data["run_id"], [s["name"] for s in data["spec"]["stages"]]


def test_continue_without_stage_runs_what_the_tree_has_left(demo: Path, capsys) -> None:
    a = seed(demo, "a", "OVER_BUDGET:pnr")
    ran(demo, a, "OVER_BUDGET:pnr", {"synth": ("done", 0), "pnr": ("over_budget", 0)})
    b = seed(demo, "b_nodw", "STOPPED")
    ran(demo, b, "STOPPED", {"synth": ("done", 0)}, names=("synth", "pnr"))
    edr(capsys, "status")
    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--dry-run")
    run_id, stages = stages_of(out)
    assert code == 0 and stages == ["export", "power"] and re.fullmatch(r"\d{8}_\d{4}_a\.export-power_demo_gabc1234", run_id)
    assert json.loads(out)["data"]["spec"]["start_at"] == {"stage": "export", "checkpoint": None}
    code, out, _ = edr(capsys, "--json", "continue", "b_nodw@demo", "--dry-run")
    assert code == 0 and stages_of(out)[1] == ["pnr"] and "_b_nodw.pnr_demo_DW0_g" in stages_of(out)[0]
    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--stage", "power", "export", "--dry-run")
    run_id, stages = stages_of(out)
    assert code == 0 and stages == ["power", "export"] and "_a.power-export_demo_g" in run_id


def test_continue_follows_the_tree_across_its_runs(demo: Path, capsys) -> None:
    a = seed(demo, "a", "OVER_BUDGET:pnr")
    ran(demo, a, "OVER_BUDGET:pnr", {"synth": ("done", 0), "pnr": ("over_budget", 0)})
    root = json.loads((bdir(demo) / f"{a}.json").read_text())["root"]
    x = seed(demo, "a.export-power", "stage:export", date="20260926_1300")
    ran(demo, x, "stage:export", {"export": ("running", None)}, names=("export", "power"), t0=200, tree=root)
    edr(capsys, "status")
    code, _, err = edr(capsys, "continue", "a@demo")
    assert code == 1 and "a.export-power@demo on the same tree has not ended" in err
    ran(demo, x, "OVER_BUDGET:export", {"export": ("over_budget", 0)}, names=("export", "power"), t0=200)
    edr(capsys, "status")
    # Both runs of the tree leave power; the second takes the tasks of the job a through its label.
    for handle, label in (("a@demo", "a.power"), ("a.export-power@demo", "a.export-power.power")):
        code, out, _ = edr(capsys, "--json", "continue", handle, "--dry-run")
        data = json.loads(out)["data"]
        assert code == 0 and data["root"] == root and f"_{label}_demo_g" in data["run_id"]
        assert [(s["name"], [t["id"] for t in s["tasks"]]) for s in data["spec"]["stages"]] == [("power", ["k_small", "k_big"])]
    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--stage", "export", "power", "--dry-run")
    assert code == 0 and "_a.export-power.2_demo_g" in stages_of(out)[0]


def test_continue_runs_exactly_the_tasks_a_group_held_back(demo: Path, capsys) -> None:
    (demo / "tasks.toml").write_text((demo / "tasks.toml").read_text() + "".join(
        f'\n[tasks.{t}]\nkernel = "gemm"\ntest = "{t.upper()}"\nargs = ""\n' for t in ("k_c", "k_d")))
    jobs = demo / "jobs" / "demo.toml"
    jobs.write_text(jobs.read_text().replace('tasks = ["k_small", "k_big"]', 'tasks = ["k_small", "k_big", "k_c", "k_d"]'))

    def group(done: tuple[str, ...], held: tuple[str, ...]) -> dict:
        """The task entries of a power group as the driver writes them after a stop."""
        return {"power": {**{t: {"phase": "done", "started": 110, "ended": 120, "exit": 0} for t in done},
                          **{t: {"phase": "held"} for t in held}}}

    def tasks_of(out: str) -> list[tuple[str, list[str]]]:
        return [(s["name"], [t["id"] for t in s["tasks"]]) for s in json.loads(out)["data"]["spec"]["stages"]]

    a = seed(demo, "a", "STOPPED")
    ran(demo, a, "STOPPED", {n: ("done", 0) for n in DEMO_STAGES})
    beat(demo, a, tasks=group(("k_small", "k_big"), ("k_c", "k_d")))
    edr(capsys, "status")
    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--dry-run")
    assert code == 0 and tasks_of(out) == [("power", ["k_c", "k_d"])]
    assert f"\n{stages_of(out)[0]}: the tasks k_c and k_d of power on local " in edr(capsys, "continue", "a@demo", "--dry-run")[1]
    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--tasks", "k_small", "--dry-run")
    assert code == 0 and tasks_of(out) == [("power", ["k_small"])]
    # The run that continues the tree starts k_c and holds k_d back again; the tree then holds k_d alone.
    root = json.loads((bdir(demo) / f"{a}.json").read_text())["root"]
    x = seed(demo, "a.power", "STOPPED", date="20260926_1300")
    ran(demo, x, "STOPPED", {"power": ("done", 0)}, names=("power",), t0=200, tree=root)
    beat(demo, x, tasks=group(("k_c",), ("k_d",)))
    edr(capsys, "status")
    jobs.write_text(jobs.read_text().replace(', "k_d"]', "]"))  # a held task runs also when the job drops it
    for handle in ("a@demo", "a.power@demo"):
        code, out, _ = edr(capsys, "--json", "continue", handle, "--dry-run")
        assert code == 0 and tasks_of(out) == [("power", ["k_d"])]


def test_only_a_task_group_holds_tasks(demo: Path, capsys) -> None:
    b = seed(demo, "b_nodw", "OVER_BUDGET:synth")
    ran(demo, b, "OVER_BUDGET:synth", {"synth": ("over_budget", 0)}, names=("synth", "pnr"))
    beat(demo, b, tasks={"synth": {"k_small": {"phase": "skipped"}}})  # synth is no task group
    edr(capsys, "status")
    code, out, _ = edr(capsys, "--json", "continue", "b_nodw@demo", "--dry-run")
    assert code == 0 and stages_of(out)[1] == ["pnr"]


def test_continue_without_stage_refuses_a_stage_that_did_not_end_with_exit_0(demo: Path, capsys) -> None:
    c = seed(demo, "c", "FAILED:pnr")
    ran(demo, c, "FAILED:pnr", {"synth": ("done", 0), "pnr": ("failed", 1)})
    d = seed(demo, "d", "FAILED:export")
    ran(demo, d, "FAILED:export", {"synth": ("done", 0), "pnr": ("done", 0), "export": ("failed", 2)})
    e = seed(demo, "e", "done")
    ran(demo, e, "done", {n: ("done", 0) for n in DEMO_STAGES})
    edr(capsys, "status")
    code, _, err = edr(capsys, "continue", "c@demo", "--dry-run")
    assert code == 1 and "c@demo: stage pnr did not end with exit 0 (failed, exit 1); name the stages with --stage, " \
                         "and a checkpoint with --from" in err
    code, _, err = edr(capsys, "continue", "d@demo", "--dry-run")
    assert code == 1 and err.rstrip().endswith("stage export did not end with exit 0 (failed, exit 2); name the stages "
                                               "with --stage")  # export has no resume command
    code, out, _ = edr(capsys, "continue", "e@demo")
    assert code == 2 and out == "e@demo: no stage left on its tree\n"
    with Database(demo / "data" / "edr.db") as db:
        assert db.events() == []


def test_the_continue_button_runs_the_stages_left_once(demo: Path, capsys, monkeypatch) -> None:
    a = seed(demo, "a", "OVER_BUDGET:pnr")
    ran(demo, a, "OVER_BUDGET:pnr", {"synth": ("done", 0), "pnr": ("over_budget", 0)})
    edr(capsys, "status")
    submitted = []
    monkeypatch.setattr(launch, "submit", lambda *args: submitted.append(args[3]))
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    root = json.loads((bdir(demo) / f"{a}.json").read_text())["root"]
    note = acts.continue_run("a@demo", "telegram")
    assert note == f"{submitted[0]}: export and power on local {root}" and "_a.export-power_" in submitted[0]
    with pytest.raises(Refuse, match="a.export-power@demo on the same tree has not ended"):
        acts.continue_run("a@demo", "telegram")  # a second tap starts no second run on the tree
    with Database(demo / "data" / "edr.db") as db:
        assert [(e["actor"], e["run_id"], e["kind"], e["text"]) for e in db.events()] == [
            ("telegram", submitted[0], "continue", f"export and power on {a}")]
    assert len(submitted) == 1


def test_a_host_prune_skips_a_tree_with_stages_left(demo: Path, capsys) -> None:
    a = seed(demo, "a", "OVER_BUDGET:pnr")
    ran(demo, a, "OVER_BUDGET:pnr", {"synth": ("done", 0), "pnr": ("over_budget", 0)})
    edr(capsys, "status")
    roots = {a: Path(json.loads((bdir(demo) / f"{a}.json").read_text())["root"])}
    code, out, _ = edr(capsys, "retire", "--host", "local", "--prune", "netlist", "--why", "full")
    assert code == 2 and out == (f"{a}: skipped, stages left on its tree: export, power\n"
                                 "no finished run with a tree on local and no stage left\n")
    d = seed(demo, "d", "done")
    ran(demo, d, "done", {n: ("done", 0) for n in DEMO_STAGES})
    edr(capsys, "status")
    roots[d] = Path(json.loads((bdir(demo) / f"{d}.json").read_text())["root"])
    code, out, _ = edr(capsys, "retire", "--host", "local", "--prune", "netlist", "--why", "full")
    assert code == 0 and f"{a}: skipped" in out and (roots[a] / "out").is_dir() and not (roots[d] / "out").exists()
    code, out, _ = edr(capsys, "retire", "a@demo", "--prune", "netlist", "--why", "x", "--dry-run")
    assert code == 0 and out.startswith(f"{a}: stages left on its tree: export, power; they may need what the prune "
                                        f"removes\n{a}: rm -rf {roots[a]}/out on local (dry)")


def test_watch_check_dry_and_once(demo: Path, capsys) -> None:
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    state = bdir(demo)
    assert edr(capsys, "watch", "--check")[0] == 1
    before = sorted(p.name for p in state.iterdir())
    code, out, _ = edr(capsys, "watch", "--once", "--dry-run")
    assert code == 0 and f"{b}: running" in out and sorted(p.name for p in state.iterdir()) == before
    assert not (demo / "data" / "board").exists()
    code, out, _ = edr(capsys, "watch", "--once")
    assert code == 0 and (state.parent / "watch.json").exists()
    assert b"b_nodw" in (demo / "data" / "board" / "status.html").read_bytes()
    assert edr(capsys, "watch", "--check")[0] == 0


def test_a_second_watcher_of_a_project_exits_2(demo: Path, capsys) -> None:
    lock = config.load_project(demo).state_dir / "watch.lock"
    fd = home.lock(lock)
    assert fd is not None and home.lock(lock) is None
    code, out, _ = edr(capsys, "watch", "--once")
    assert code == 2 and f"pid {os.getpid()} watches demo already" in out and not (demo / "data" / "board").exists()
    os.close(fd)
    assert edr(capsys, "watch", "--once")[0] == 0 and home.lock(lock) is not None


def test_register_links_the_project_and_refuses_a_second_directory(demo: Path, capsys, tmp_path: Path,
                                                                   user_root: Path, monkeypatch) -> None:
    link = user_root / "projects" / "demo"
    assert edr(capsys, "register", "--dry-run")[0] == 0 and not link.exists()
    code, out, _ = edr(capsys, "register")
    assert code == 0 and link.resolve() == demo.resolve() and edr(capsys, "register")[0] == 2
    twin = tmp_path / "edr" / "twin"
    shutil.copytree(demo, twin, ignore=shutil.ignore_patterns("data"))
    monkeypatch.chdir(twin)
    why = f"the project name demo belongs to {demo.resolve()}; rename `project` in {twin / 'edr.toml'}"
    for argv in (["register"], ["unregister"], ["launch", "demo"], ["watch", "--once"]):
        code, _, err = edr(capsys, *argv)
        assert code == 1 and why in err, argv
    code, out, _ = edr(capsys, "check")
    assert code == 1 and why in out
    assert not (twin / "data").exists() and not (config.load_project(twin).state_dir / "watch.json").exists()
    monkeypatch.chdir(demo)
    assert edr(capsys, "unregister")[0] == 0 and not link.exists() and edr(capsys, "unregister")[0] == 2
    (demo / "edr.toml").write_text((demo / "edr.toml").read_text().replace('project = "demo"', 'project = "moved"'))
    link.symlink_to(demo)  # a link whose directory now holds another project frees the name
    monkeypatch.chdir(twin)
    assert edr(capsys, "register")[0] == 0 and link.resolve() == twin.resolve()


def test_a_project_by_name_from_any_directory(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    seed(demo, "a", "done")
    assert edr(capsys, "register")[0] == 0
    monkeypatch.chdir(tmp_path)
    code, out, _ = edr(capsys, "-P", "demo", "status")
    assert code == 0 and "done" in out
    code, _, err = edr(capsys, "-P", "nope", "status")
    assert code == 1 and "no registered project nope" in err
    monkeypatch.setenv("EDR_PROJECT", "demo")
    assert edr(capsys, "status")[0] == 0
    other = tmp_path / "other"
    other.mkdir()
    (other / "edr.toml").write_text('project = "other"\n')
    monkeypatch.chdir(other)
    code, _, err = edr(capsys, "status")
    assert code == 1 and f"inside the project {other}" in err
    assert edr(capsys, "-P", "demo", "status")[0] == 0


def test_projects_and_the_board_of_every_project(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    beta = tmp_path / "edr" / "beta"
    shutil.copytree(demo, beta, ignore=shutil.ignore_patterns("data"))
    (beta / "edr.toml").write_text((beta / "edr.toml").read_text().replace('project = "demo"', 'project = "beta"'))
    seed(demo, "a", "stage:synth")
    seed(demo, "d", "done")
    assert edr(capsys, "register")[0] == 0
    monkeypatch.chdir(beta)
    assert edr(capsys, "register")[0] == 0
    (beta / "data").mkdir()
    with Database(beta / "data" / "edr.db") as db:
        db.upsert_run({"run_id": f"{DATE}_q_demo_gabc1234", "batch": "demo", "label": "q", "state": "queued"})
    monkeypatch.chdir(tmp_path)
    state = config.load_project(demo).state_dir
    # The fresh watch.json of a watcher that is gone names no watcher; the process that holds watch.lock does.
    config.save_json(state / "watch.json", {"ts": time.time(), "cycle": 1, "pid": 4711})
    assert "pid 4711" not in edr(capsys, "projects")[1]
    fd = home.lock(state / "watch.lock")
    code, out, _ = edr(capsys, "--json", "projects")
    rows = {r["project"]: r for r in json.loads(out)["data"]}
    assert code == 0 and (rows["demo"]["watcher"], rows["demo"]["live"], rows["beta"]["watcher"], rows["beta"]["live"]) == (
        str(os.getpid()), 1, None, 0)
    assert rows["demo"]["root"] == str(demo.resolve()) and rows["beta"]["note"] == ""
    (beta / "edr.toml").write_text((beta / "edr.toml").read_text() + "\nbogus = 1\n")
    code, out, _ = edr(capsys, "projects")
    os.close(fd)
    assert code == 0 and "unknown key 'metrics.energy_nj.bogus'" in out and f"pid {os.getpid()}" in out
    (beta / "edr.toml").write_text((beta / "edr.toml").read_text().replace("\nbogus = 1\n", ""))
    code, out, _ = edr(capsys, "--json", "status", "--all")
    runs = json.loads(out)["data"]["runs"]
    assert code == 0 and sorted((r["project"], r["label"]) for r in runs) == [("beta", "q"), ("demo", "a"), ("demo", "d")]
    code, out, _ = edr(capsys, "status", "--all")
    assert out.splitlines()[0].split()[:2] == ["project", "label"] and not (beta / "data" / "board").exists()


def _edr_bytes(demo: Path, *argv: str, **env: str) -> bytes:
    """edr in a subprocess with stdout a pipe; the colour variables of the caller stay out."""
    clean = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "TTY_COMPATIBLE", "COLUMNS")}
    p = subprocess.run([sys.executable, "-m", "edarunner.cli", *argv], cwd=demo, env={**clean, **env},
                       capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr.decode()
    return p.stdout


def test_pipe_and_no_color_carry_no_escape_codes(demo: Path) -> None:
    a = seed(demo, "a", "done")
    seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("user", a, "launch", "local /x")
    for argv in (["status"], ["status", "--narrow"], ["status", "--triage"], ["status", "a@demo"], ["hosts"], ["tools"],
                 ["events"], ["check"]):
        assert b"\x1b" not in _edr_bytes(demo, *argv), argv
        assert b"\x1b" not in _edr_bytes(demo, *argv, NO_COLOR="1", FORCE_COLOR="1", TERM="xterm-256color"), argv
    # A forced terminal proves the colour path is live; NO_COLOR above switched it off.
    assert b"\x1b[32m" in _edr_bytes(demo, "status", FORCE_COLOR="1", TERM="xterm-256color")
    narrow = _edr_bytes(demo, "status", "--narrow", FORCE_COLOR="1", TERM="xterm-256color").decode()
    assert b"\x1b" in narrow.encode() and all(len(re.sub(r"\x1b\[[0-9;]*m", "", ln)) <= 48 for ln in narrow.splitlines())


def test_json_output_is_the_same_bytes_with_and_without_colour(demo: Path, capsys, monkeypatch) -> None:
    a = seed(demo, "a", "done")
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("user", a, "launch", "local /x")
    # A live probe of the local host changes between two calls; a fixed one keeps the bytes comparable.
    monkeypatch.setattr(Ssh, "probe", lambda self, host: HostProbe(host, 4.0, 8.0, "/tmp/x", 50.0, cores=4, total_gb=60.0))
    for var in ("NO_COLOR", "FORCE_COLOR", "TTY_COMPATIBLE", "COLUMNS"):
        monkeypatch.delenv(var, raising=False)
    for argv in (["status"], ["hosts"], ["tools"], ["events"], ["check"], ["status", "a@demo"]):
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        plain = edr(capsys, "--json", *argv)[1]
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("TERM", "xterm-256color")
        forced = edr(capsys, "--json", *argv)[1]
        assert plain == forced and "\x1b" not in plain, argv
        env = json.loads(plain)
        assert env["code"] == 0 and env["output"] == "" and env["data"], argv


def test_bad_input_exits_1(capsys) -> None:
    assert cli.main([]) == 1 and cli.main(["nope"]) == 1 and cli.main(["stop", "x"]) == 1
    assert cli.main(["metrics"]) == 1 and cli.main(["--help"]) == 0
    capsys.readouterr()


def test_import_records_a_foreign_tree(demo: Path, capsys, tmp_path: Path) -> None:
    root = tmp_path / "scratch" / "user" / "edr" / "old" / "20260904_0411_ref_x_gabc1234"
    root.mkdir(parents=True)
    argv = ["import", "--run-id", root.name, "--label", "ref", "--config", "demo", "--source", "abc1234",
            "--host", "local", "--root", str(root), "--why", "reference"]
    code, out, _ = edr(capsys, *argv, "--dry-run")
    assert code == 0 and "(dry)" in out and not (demo / "data" / "edr.db").exists()
    assert edr(capsys, *argv)[0] == 0
    with Database(demo / "data" / "edr.db") as db:
        row = db.run(root.name)
        assert row["phase"] == "done" and row["state"] == "imported" and row["root"] == str(root)
        assert [b["batch"] for b in db.batches()] == ["imported"]
        assert db.events()[-1]["kind"] == "import"
    assert edr(capsys, "import", "--run-id", "bad", "--label", "r", "--config", "demo", "--source", "a",
               "--host", "local", "--root", str(root))[0] == 1
    assert edr(capsys, "import", "--run-id", root.name, "--label", "r", "--config", "demo", "--source", "a",
               "--host", "local", "--root", str(root / "missing"))[0] == 1
    other = root.with_name("20260904_0412_noconf_x_gabc1234")
    other.mkdir()
    assert edr(capsys, "import", "--run-id", other.name, "--label", "noconf", "--source", "abc1234", "--host", "local",
               "--root", str(other))[0] == 0  # a job's config is optional, so it is here too


def test_an_incomplete_phase_carries_the_held_tasks() -> None:
    project = config.load_project(DEMO)
    cli._check_phase(project, "INCOMPLETE:2f0s1h")
    for bad in ("INCOMPLETE:0f0s0h", "INCOMPLETE:1f0s"):
        with pytest.raises(Refuse, match="INCOMPLETE:<n>f<m>s<k>h with a task that failed, was skipped or was held"):
            cli._check_phase(project, bad)


def test_status_follows_the_heartbeat_between_watcher_cycles(demo: Path, capsys) -> None:
    now = int(time.time())
    b = seed(demo, "b_nodw", "setup", state=None, updated=now - 5000)
    hb_path = bdir(demo) / f"{b}.json"
    hb = json.loads(hb_path.read_text())
    hb.update(phase="stage:synth", stage="synth", updated=now, counts={"done": 0, "failed": 0})
    hb_path.write_text(json.dumps(hb))
    code, out, _ = edr(capsys, "--json", "status")
    row = {r["run_id"]: r for r in json.loads(out)["data"]["runs"]}[b]
    assert code == 0 and row["state"] == "running" and now - row["updated"] < 60 and row["phase"] == "stage:synth"
    hb.update(updated=now - 100000)
    hb_path.write_text(json.dumps(hb))
    row = {r["run_id"]: r for r in json.loads(edr(capsys, "--json", "status")[1])["data"]["runs"]}[b]
    assert row["state"] == "dead"
    hb.update(phase="gate:synth", gate="demo: 0 free, 1 held by others, 1 needed", updated=now)
    hb_path.write_text(json.dumps(hb))
    code, out, _ = edr(capsys, "status", "b_nodw@demo")
    assert code == 0 and "gate waits for demo: 0 free, 1 held by others, 1 needed" in out
    hb.update(phase="INCOMPLETE:1f0s", exit=8, updated=now, counts={"done": 0, "failed": 1})
    hb_path.write_text(json.dumps(hb))
    code, out, _ = edr(capsys, "status")
    assert code == 0 and "INCOMPLETE:1f0s" in out
    code, out, _ = edr(capsys, "status", "--triage")
    assert code == 0 and "incomplete" in out and "b_nodw@demo" in out
    code, out, _ = edr(capsys, "status", "b_nodw@demo")
    assert code == 0 and "INCOMPLETE:1f0s" in out
    with Database(demo / "data" / "edr.db") as db:
        assert db.run(b)["phase"] == "INCOMPLETE:1f0s" and db.run(b)["exit"] == 8


def test_run_on_an_imported_tree_needs_no_jobs_file(demo: Path, capsys, tmp_path: Path) -> None:
    root = tmp_path / "scratch" / "user" / "edr" / "old" / "20260904_0411_ref_demo_gabc1234"
    root.mkdir(parents=True)
    assert edr(capsys, "import", "--run-id", root.name, "--label", "ref", "--config", "demo", "--source", "abc1234",
               "--host", "local", "--root", str(root))[0] == 0
    code, out, err = edr(capsys, "continue", "ref@imported", "--stage", "power", "--tasks", "k_small", "--on", "local",
                         "--dry-run")
    assert code == 0, (out, err)
    assert "(dry)" in out and "ref.power" in out


def test_retire_collects_the_named_lists_first(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    root = Path(config.load_project(demo).site.scratch[0]) / getpass.getuser() / "edr" / "demo" / a
    results = demo / "data" / "results" / a
    (results / "log").mkdir(parents=True)
    with_vars(demo, a)
    code, out, _ = edr(capsys, "retire", "a@demo", "--collect", "netlist", "--why", "x", "--dry-run")
    assert code == 0 and "collect netlist: 1 files (dry)" in out and root.is_dir() and not (results / "out").exists()
    code, _, err = edr(capsys, "retire", "a@demo", "--collect", "netlist,nope", "--why", "x")
    assert code == 1 and "collect nope" in err and "nothing removed" in err and root.is_dir()
    code, out, _ = edr(capsys, "retire", "a@demo", "--collect", "netlist", "--why", "x")
    assert code == 0 and not root.exists() and (results / "out" / "11" / "netlist.v").is_file()
    with Database(demo / "data" / "edr.db") as db:
        assert [e["kind"] for e in db.events(run_id=a)] == ["collect", "collect", "retire"]


def test_retire_refuses_a_young_run_without_a_heartbeat(demo: Path, capsys) -> None:
    n = seed(demo, "n", None, started=int(time.time()))  # the demo dead_s is 90 s
    (bdir(demo) / f"{n}.json").unlink()
    root = Path(config.load_project(demo).site.scratch[0]) / getpass.getuser() / "edr" / "demo" / n
    code, _, err = edr(capsys, "retire", "n@demo", "--uncollected", "--why", "x")
    assert code == 1 and "no heartbeat yet" in err and root.is_dir()
    code, _, err = edr(capsys, "retire", "n@demo", "--prune", "netlist", "--why", "x")
    assert code == 1 and "no heartbeat yet" in err and (root / "out").is_dir()
    with Database(demo / "data" / "edr.db") as db:
        db.upsert_run({"run_id": n, "started": int(time.time()) - 3000})
    code, _, err = edr(capsys, "retire", "n@demo", "--uncollected", "--why", "x")
    assert code == 0, err
    assert not root.exists()


def test_retire_refuses_a_root_that_another_run_uses(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    with Database(demo / "data" / "edr.db") as db:
        row = db.run(a)
        db.upsert_run({"run_id": f"{DATE}_b_nodw_demo_gabc1234", "batch": "demo", "label": "b_nodw", "config": "demo",
                        "host": row["host"], "root": row["root"], "source": "abc1234", "phase": "done", "state": "done"})
    code, out, err = edr(capsys, "retire", "a@demo", "--why", "t", "--uncollected")
    assert code == 1 and "results are not collected" in err
    assert Path(row["root"]).exists()
    code, out, err = edr(capsys, "retire", "a@demo", "--why", "t", "--prune", "netlist", "--dry-run")
    assert code == 0, err  # a prune of a shared root is fine while no sharer is live
    (demo / "data" / "results" / f"{DATE}_b_nodw_demo_gabc1234" / "log").mkdir(parents=True)
    code, out, err = edr(capsys, "retire", "a@demo", "--why", "t", "--uncollected", "--dry-run")
    assert code == 0, err  # the sharer is finished and collected, so the tree may go
    code, out, err = edr(capsys, "retire", "--batch", "demo", "--why", "t", "--uncollected")
    assert code == 0, err
    assert not Path(row["root"]).exists()


def test_retire_batch_removes_the_checked_out_tree_no_other_batch_uses(demo: Path, capsys) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=demo, check=True, capture_output=True)
    repo = demo / "repo"
    source = json.loads(edr(capsys, "--json", "checkout", "HEAD")[1])["data"]["source"]
    wt = demo / "wt" / source
    seed(demo, "a", "done", source=source)
    seed(demo, "b", "done", source=source, batch="other")
    assert edr(capsys, "retire", "--batch", "other", "--uncollected", "--why", "x")[0] == 0
    assert (wt / ".git").is_dir()  # demo still has the source
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "x", "--dry-run")
    assert code == 0 and f"rm -rf {wt} (dry)" in out and (wt / ".git").is_dir()
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "x")
    assert code == 0 and not wt.exists() and repo.is_dir()
    with open(repo / "flow" / "flow.sh", "a") as f:
        f.write("# dirty\n")
    dirty = json.loads(edr(capsys, "--json", "checkout", "--dirty", str(repo))[1])["data"]["source"]
    snap = demo / "wt" / dirty
    assert "-dirty-" in dirty and snap.is_dir() and (snap / ".git").is_dir()
    seed(demo, "c", "done", source=dirty, batch="snap")
    code, out, _ = edr(capsys, "retire", "--batch", "snap", "--uncollected", "--why", "y")
    assert code == 0 and f"rm -rf {snap}" in out and not snap.exists() and (repo / "flow").is_dir()
    with Database(demo / "data" / "edr.db") as db:
        assert [e["text"] for e in db.events() if e["run_id"] == ""] == [f"x: worktree {wt}", f"y: worktree {snap}"]


def test_retire_batch_keeps_a_worktree_without_the_marker(demo: Path, capsys, tmp_path: Path) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=demo, check=True, capture_output=True)
    toml = demo / "edr.toml"
    toml.write_text(toml.read_text().replace('worktrees = "wt"', f'worktrees = "{tmp_path / "rtl-wt"}"'))
    source = json.loads(edr(capsys, "--json", "checkout", "HEAD")[1])["data"]["source"]
    wt = tmp_path / "rtl-wt" / source
    a = seed(demo, "a", "done", source=source)
    with Database(demo / "data" / "edr.db") as db:
        root = Path(db.run(a)["root"])
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "x")
    assert code == 0 and f"demo: worktree kept: '{wt}' does not contain the marker '/edr/'; " \
        "remove it with rm -rf" in out
    assert (wt / ".git").is_dir() and not root.exists() and f"rm -rf {root}" in out
    with Database(demo / "data" / "edr.db") as db:
        assert [e["kind"] for e in db.events(run_id=a)] == ["retire"] and db.batches()[0]["retired"]
        assert not [e for e in db.events() if e["run_id"] == ""]  # no worktree event


def test_import_results_links_and_extracts(demo: Path, capsys, tmp_path: Path) -> None:
    run_id = "20260904_0411_ref_demo_gabc1234"
    src = tmp_path / "legacy" / run_id
    (src / "reports" / "3").mkdir(parents=True)
    (src / "reports" / "3" / "area.rpt").write_text("i_top 1000.0\n")
    p = src / "simulation" / "tests" / "demo" / "GEMM_M64_N64" / "power"
    (p / "reports").mkdir(parents=True)
    (p / "reports" / "power.csv").write_text("phase,total_w\nWHOLE,0.250\n")
    (p / "phases.json").write_text('{"window_ns": 3400}')
    base = ["import", "--run-id", run_id, "--label", "ref", "--config", "demo", "--source", "abc1234"]
    assert edr(capsys, *base)[0] == 1  # neither a tree nor results
    assert edr(capsys, *base, "--results", str(src), "--tasks", "nope")[0] == 1
    code, out, _ = edr(capsys, *base, "--results", str(src), "--tasks", "k_small", "--dry-run")
    assert code == 0 and "(dry)" in out and not (demo / "data").exists()
    # The spec of an import lists its task group only; its synth log is read all the same.
    (src / "log").mkdir()
    (src / "log" / "synth.log").write_text("set DW 0;\n")
    toml = demo / "edr.toml"
    toml.write_text(toml.read_text() + "\n[parameters.knobs]\nstage = \"synth\"\nfile = \"log/synth.log\"\n"
                    "regex = 'set (?P<key>\\w+) (?P<value>[^;]+);'\n")
    code, out, _ = edr(capsys, *base, "--results", str(src), "--tasks", "k_small", "--param", "DW=1")
    link = demo / "data" / "results" / run_id
    assert code == 0 and "4 metrics" in out and link.is_symlink() and link.resolve() == src.resolve()
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234", "--csv")
    got = {ln.split(",")[9]: ln.split(",")[11] for ln in out.splitlines()[1:]}
    assert code == 0 and got == {"area_cell_um2": "1000.0", "power_w": "0.25", "window_ns": "3400.0", "energy_nj": "850.0"}
    with Database(demo / "data" / "edr.db") as db:
        row = db.run(run_id)
        assert row["root"] is None and row["host"] == "" and row["state"] == "imported"
        assert db.events()[-1]["text"].endswith("4 metrics")
        assert [(f["check"], f["text"]) for f in db.flags([run_id])] == [
            ("declared_vs_observed", "DW is 0 in the run's files and 1 by import")]
    other = tmp_path / "other"
    other.mkdir()
    assert edr(capsys, *base, "--results", str(other))[0] == 1  # never replaces a linked tree
    assert edr(capsys, "continue", "ref@imported", "--stage", "power", "--tasks", "k_small", "--on", "local", "--dry-run")[0] == 1
    exp = tmp_path / "exp"
    code, out, _ = edr(capsys, "export", "--source", "abc1234", "--out", str(exp), "--files")
    assert code == 0 and (exp / run_id / "reports" / "3" / "area.rpt").is_file()


def test_import_records_what_it_is_told(demo: Path, capsys, tmp_path: Path) -> None:
    run_id = "20260830_0900_ref_demo_gabc1234-dirty"
    src = tmp_path / "legacy" / run_id
    tests = src / "simulation" / "tests" / "demo"
    # k_small parses; power.csv of k_big has no WHOLE row; k_bad has no file at all.
    for test, text in (("GEMM_M64_N64", "phase,total_w\nWHOLE,0.250\n"), ("SOFTMAX_N512", "phase,total_w\n")):
        (tests / test / "power" / "reports").mkdir(parents=True)
        (tests / test / "power" / "reports" / "power.csv").write_text(text)
        (tests / test / "power" / "phases.json").write_text('{"window_ns": 3400}')
    start = int(datetime.fromisoformat("2026-08-30T09:00").timestamp())
    base = ["import", "--run-id", run_id, "--label", "ref", "--config", "demo", "--source", "abc1234-dirty",
            "--results", str(src), "--tasks", "k_small", "k_big", "k_bad", "--batch", "legacy"]
    for bad in (["--phase", "INCOMPLETE:0f0s"], ["--phase", "FAILED:nope"], ["--phase", "done:"],
                ["--started", "yesterday"], ["--started", str(start), "--ended", str(start - 1)], ["--param", "source=x"]):
        assert edr(capsys, *base, *bad)[0] == 1, bad
    assert "a run whose driver died is FAILED:<stage>" in edr(capsys, *base, "--phase", "INCOMPLETE:0f0s")[2]
    assert not (demo / "data").exists()
    code, out, _ = edr(capsys, *base, "--phase", "FAILED:power", "--host", "hostA", "--started", "2026-08-30T09:00",
                       "--ended", str(start + 3600), "--param", "vars.netlist_stage=11")
    assert code == 0 and "warning: abc1234-dirty does not have the form of an edr checkout tag" in out
    spec = json.loads((bdir(demo, "legacy") / f"{run_id}.spec.json").read_text())
    assert spec["vars"] == {"netlist_stage": "11"} and not any("cmd" in st for st in spec["stages"])
    assert [(t["id"], t["dir"]) for st in spec["stages"] for t in st["tasks"]] == [
        ("k_small", "simulation/tests/demo/GEMM_M64_N64"), ("k_big", "simulation/tests/demo/SOFTMAX_N512"),
        ("k_bad", "simulation/tests/demo/BAD")]
    with Database(demo / "data" / "edr.db") as db:
        row = db.run(run_id)
        assert (row["dirty"], row["host"], row["phase"], row["started"], row["updated"]) == (
            1, "hostA", "FAILED:power", start, start + 3600)
        assert row["counts"] == {"done": 1, "failed": 2}
        assert {(p["key"], p["value"]) for p in db.parameters(run_id)} == {
            ("config", "demo"), ("source", "abc1234-dirty"), ("vars.netlist_stage", "11")}
        db.upsert_batch({"batch": "legacy", "created": 1})
    # A second source in the batch is refused; one more run of its source leaves the batch row as it is.
    other = ["import", "--label", "ref2", "--config", "demo", "--results", str(src), "--batch", "legacy"]
    code, _, err = edr(capsys, *other, "--run-id", "20260830_0901_ref2_demo_gdef5678", "--source", "def5678")
    assert code == 1 and "batch legacy holds the source abc1234-dirty" in err
    code, out, _ = edr(capsys, *other, "--run-id", "20260830_0901_ref2_demo_gabc1234-dirty", "--source", "abc1234-dirty")
    assert code == 0
    code, out, _ = edr(capsys, "import", "--run-id", "20260830_0902_ref3_demo_gdef5678", "--label", "ref3",
                       "--source", "def5678", "--results", str(src), "--batch", "clean")
    assert code == 0 and "warning" not in out
    launched = seed(demo, "a", "done")
    code, _, err = edr(capsys, "import", "--run-id", launched, "--label", "a", "--source", "abc1234", "--results", str(src),
                       "--batch", "demo")
    assert code == 1 and "exists in state running" in err
    with Database(demo / "data" / "edr.db") as db:
        assert [(b["batch"], b["source"], b["created"]) for b in db.batches() if b["batch"] == "legacy"] == [
            ("legacy", "abc1234-dirty", 1)]
        assert (db.run("20260830_0902_ref3_demo_gdef5678")["dirty"], db.run("20260830_0901_ref2_demo_gabc1234-dirty")[
            "started"], db.run("20260830_0901_ref2_demo_gabc1234-dirty")["updated"]) == (0, None, None)
    # Once its files are fixed, a later extract reads every task of the spec, also the one without a row.
    (tests / "SOFTMAX_N512" / "power" / "reports" / "power.csv").write_text("phase,total_w\nWHOLE,0.5\n")
    (tests / "BAD" / "power" / "reports").mkdir(parents=True)
    (tests / "BAD" / "power" / "reports" / "power.csv").write_text("phase,total_w\nWHOLE,0.125\n")
    assert edr(capsys, "extract", "ref@legacy")[0] == 0
    with Database(demo / "data" / "edr.db") as db:
        got = {(m["task"], m["name"]): m["value"] for m in db.metrics(run_ids=[run_id])}
    assert (got[("k_big", "power_w")], got[("k_big", "energy_nj")], got[("k_bad", "power_w")]) == (0.5, 1700.0, 0.125)


def test_import_records_task_fields_and_a_task_with_two_sets_of_fields_warns(demo: Path, capsys, tmp_path: Path) -> None:
    src = tmp_path / "legacy"
    for test in ("GEMM_M64_N64", "SOFTMAX_OLD"):
        (src / "simulation" / "tests" / "demo" / test / "power" / "reports").mkdir(parents=True)
        (src / "simulation" / "tests" / "demo" / test / "power" / "reports" / "power.csv").write_text("phase,total_w\nWHOLE,0.5\n")
    # k_big ran under an older test name and with a limit that tasks.toml no longer gives; k_gone did not run.
    ran = tmp_path / "ran.toml"
    ran.write_text('[tasks.k_big]\nkernel = "softmax"\ntest = "SOFTMAX_OLD"\nargs = "N=512 LIMIT=-4.0"\n\n'
                   '[tasks.k_gone]\nkernel = "x"\n')
    old, new = "20260830_0900_ref_demo_gabc1234", "20260830_0901_ref2_demo_gabc1234"
    base = ["import", "--config", "demo", "--source", "abc1234", "--results", str(src), "--batch", "legacy"]
    assert edr(capsys, *base, "--run-id", old, "--label", "ref", "--task-fields", str(ran))[0] == 1  # no --tasks
    assert edr(capsys, *base, "--run-id", old, "--label", "ref", "--tasks", "k_big", "--task-fields", str(tmp_path / "no.toml"))[0] == 1
    code, out, _ = edr(capsys, *base, "--run-id", old, "--label", "ref", "--tasks", "k_small", "k_big", "--task-fields", str(ran))
    assert code == 0
    spec = json.loads((bdir(demo, "legacy") / f"{old}.spec.json").read_text())
    assert [(t["id"], t["dir"], t["fields"]["test"]) for st in spec["stages"] for t in st["tasks"]] == [
        ("k_small", "simulation/tests/demo/GEMM_M64_N64", "GEMM_M64_N64"), ("k_big", "simulation/tests/demo/SOFTMAX_OLD", "SOFTMAX_OLD")]
    with Database(demo / "data" / "edr.db") as db:
        assert [(f["task"], f["key"], f["value"], f["origin"]) for f in db.task_fields([old])] == [
            ("k_big", "args", "N=512 LIMIT=-4.0", "import"), ("k_big", "kernel", "softmax", "import"),
            ("k_big", "test", "SOFTMAX_OLD", "import"), ("k_small", "args", "M=64 N=64", "resolver"),
            ("k_small", "kernel", "gemm", "resolver"), ("k_small", "test", "GEMM_M64_N64", "resolver")]
        # The numbers of k_big come from the directory of the test it ran as.
        assert {m["task"] for m in db.metrics(run_ids=[old], name="power_w")} == {"k_small", "k_big"}
    code, out, _ = edr(capsys, "extract", "--batch", "legacy", "--dry-run")
    assert code == 0 and "warning" not in out
    # A second run of k_big with the fields that tasks.toml gives today: one task id, two sets of fields.
    assert edr(capsys, *base, "--run-id", new, "--label", "ref2", "--tasks", "k_big")[0] == 0
    warning = ('task k_big ran with 2 sets of fields: args="N=512 LIMIT=-4.0" test="SOFTMAX_OLD" in ref@legacy; '
               'args="N=512" test="SOFTMAX_N512" in ref2@legacy')
    code, out, _ = edr(capsys, "extract", "ref2@legacy", "--dry-run")
    assert code == 0 and out.splitlines()[-1] == f"warning: {warning}"
    code, out, _ = edr(capsys, "--json", "extract", "ref2@legacy", "--dry-run")
    (clash,) = json.loads(out)["data"][0]["clashes"]
    assert clash["task"] == "k_big" and [s["runs"] for s in clash["sets"]] == [[old], [new]]
    code, out, _ = edr(capsys, "--json", "check")
    assert code == 0 and json.loads(out)["data"]["warnings"] == [warning]


def test_coverage_says_where_each_row_of_a_demand_is(demo: Path, capsys, tmp_path: Path) -> None:
    demand = tmp_path / "demand.csv"
    demand.write_text("label,build_tag,stage,task,source,note\n"
                      "alpha,,power,k_a,abc1234,held\n"
                      "alpha,,power,k_b,abc1234,only at def5678\n"
                      "alpha,,power,k_c,abc1234,no run\n"
                      "beta,,power,k_a,abc1234,\n"
                      "gamma,,power,k_a,abc1234,\n"
                      "delta,,power,k_a,abc1234,held by the run that continues delta\n"
                      ",bt_a,bench,k_a,abc1234,the bench run of the build\n"
                      "alpha,,power,k_b,,any source\n")
    assert edr(capsys, "coverage", str(demand))[0] == 1 and not (demo / "data").exists()
    done = seed(demo, "alpha", "done", tree=False, build_tag="bt_a")
    other = seed(demo, "alpha", "done", source="def5678", tree=False, build_tag="bt_a")
    # A newer rerun at the same source failed; the pick stays on the done run.
    seed(demo, "alpha", "FAILED:power", date="20260926_1300", tree=False, build_tag="bt_a", started=int(time.time()))
    bench = seed(demo, "alpha_rtl", "done", tree=False, build_tag="bt_a")
    seed(demo, "beta", "FAILED:pnr", tree=False)
    seed(demo, "gamma", "power:k_a", tree=False)
    seed(demo, "delta", "FAILED:power", tree=False)
    resumed = seed(demo, "delta.power", "done", tree=False)
    with Database(demo / "data" / "edr.db") as db:
        for run, stage, task in ((done, "power", "k_a"), (other, "power", "k_a"), (other, "power", "k_b"), (bench, "bench", "k_a"),
                                 (resumed, "power", "k_a")):
            db.add_metric({"run_id": run, "stage": stage, "task": task, "name": "energy_nj", "value": 1.0})
    code, out, _ = edr(capsys, "coverage", str(demand))
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 1 and lines[0] == ["label", "build_tag", "stage", "task", "source", "status", "runs"]
    assert lines[2:] == [["alpha", "power", "k_a", "abc1234", "held", "alpha@abc1234"],
                         ["alpha", "power", "k_b", "abc1234", "elsewhere", "alpha@def5678"],
                         ["alpha", "power", "k_c", "abc1234", "missing"],
                         ["beta", "power", "k_a", "abc1234", "failed", "beta@abc1234", "FAILED:pnr"],
                         ["gamma", "power", "k_a", "abc1234", "running", "gamma@abc1234", "power:k_a"],
                         ["delta", "power", "k_a", "abc1234", "held", "delta.power@abc1234"],
                         ["bt_a", "bench", "k_a", "abc1234", "held", "alpha_rtl@abc1234"],
                         ["alpha", "power", "k_b", "held", "alpha@def5678"],
                         ["4", "of", "8", "rows", "held"]]
    rows = json.loads(edr(capsys, "--json", "coverage", str(demand))[1])["data"]
    assert rows[1]["runs"] == [{"run_id": other, "label": "alpha", "source": "def5678", "phase": "done"}]
    demand.write_text("label,stage,task\nalpha,power,k_a\n")
    assert edr(capsys, "coverage", str(demand))[1].splitlines()[-1] == "1 of 1 rows held"
    for text, err in (("label,stage\nalpha,power\n", "the header needs"), ("label,stage,task\nalpha,,k_a\n", "line 2 needs")):
        demand.write_text(text)
        code, _, msg = edr(capsys, "coverage", str(demand))
        assert code == 1 and msg.startswith(f"edr: {demand}: {err}")


def test_notify_sends_one_message_through_every_notifier(demo: Path, capsys, monkeypatch) -> None:
    code, out, _ = edr(capsys, "notify", "--dry-run", "session x: <done>")
    assert code == 0 and out == "<b>demo: note</b>\nsession x: &lt;done&gt;\n(dry)\n"
    code, _, err = edr(capsys, "notify", "hi")
    assert code == 1 and "no notifier is configured" in err and not (demo / "data" / "edr.db").exists()
    posts: list[tuple] = []

    class Rec(Notifier):
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        def post(self, title, html, silent=False):
            posts.append((title, html, silent))
            return self.ok

    monkeypatch.setattr(cli, "make_notifiers", lambda *a: [Rec(True), Rec(True)])
    code, out, _ = edr(capsys, "--json", "notify", "--silent", "a & b")
    assert code == 0 and json.loads(out)["data"] == {"sent": 2, "text": "a & b"}
    assert posts == [("note", "a &amp; b", True)] * 2
    monkeypatch.setattr(cli, "make_notifiers", lambda *a: [Rec(True), Rec(False)])
    assert edr(capsys, "notify", "x")[0] == 1


def test_log_tail_fetches_the_last_lines_from_the_host(demo: Path) -> None:
    log = demo.parent / "synth.log"
    log.write_text("".join(f"line {n}\n" for n in range(10)))
    hb_file = bdir(demo) / f"{seed(demo, 'a', 'stage:synth', pid=os.getpid())}.json"
    hb_file.write_text(json.dumps({**json.loads(hb_file.read_text()), "log": str(log)}))
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert acts.log_tail("a@demo", 3) == ("a@demo.log", b"line 7\nline 8\nline 9\n")
    log.unlink()
    with pytest.raises(HostError, match="tail"):
        acts.log_tail("a@demo", 3)


def test_status_digest_prints_the_digest_as_text(demo: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    seed(demo, "a", "done")
    code, out, _ = edr(capsys, "status", "--digest")
    assert code == 0 and out.startswith("since ") and "demo\nended\n⚪ a@demo done" in out and "<" not in out
    code, out, _ = edr(capsys, "--json", "status", "--digest")
    assert code == 0 and "<code>a@demo</code>" in json.loads(out)["data"]["digest"]
    monkeypatch.chdir(tmp_path)
    assert edr(capsys, "status", "--digest", "--all")[1] .startswith("since ")  # no project registered yet
    monkeypatch.chdir(demo)
    assert edr(capsys, "register")[0] == 0
    monkeypatch.chdir(tmp_path)
    assert "⚪ a@demo done" in edr(capsys, "status", "--digest", "--all")[1]


# track

FLOW = ["bash", "flow/flow.sh", "synth", "x", "demo", "LAST_STAGE=synth"]


def track(*argv: str) -> subprocess.CompletedProcess:
    """edr track in its own process, so its exec replaces that process and not the test."""
    return subprocess.run([sys.executable, "-m", "edarunner.cli", "track", *argv], capture_output=True, text=True,
                          timeout=120)


def test_track_dry_run_prints_the_spec_and_writes_nothing(demo: Path, capsys) -> None:
    code, out, _ = edr(capsys, "track", "--label", "t", "--stage", "synth", "--source", "abc1234", "--dry-run", "--", *FLOW)
    spec = json.loads(out[out.index("{"):])
    (st,) = spec["stages"]
    assert code == 0 and st["cmd"] == shlex.join(FLOW) and "resume" not in st and st["steps"][0] == "setup"
    assert spec["batch"] == "track" and spec["host"] is None and spec["root"] == str(demo) and spec["collect"] is False
    assert spec["run_id"].endswith("_t_track_gabc1234") and spec["start_at"] == {"stage": "synth", "checkpoint": None}
    assert spec["config"] == "t"
    assert not (Path.home() / ".edr").exists() and not (demo / "data" / "edr.db").exists()
    code, out, _ = edr(capsys, "track", "--label", "t", "--stage", "synth", "--source", "abc1234", "--build-tag", "bt_a",
                       "--config", "cfg_a", "--dry-run", "--", "true")
    spec = json.loads(out[out.index("{"):])
    assert code == 0 and spec["run_id"].endswith("_t_bt_a_gabc1234") and spec["config"] == "cfg_a"
    code, _, err = edr(capsys, "track", "--label", "t", "--stage", "power", "--source", "a", "--dry-run", "--", "true")
    assert code == 1 and "task group" in err
    code, _, err = edr(capsys, "track", "--label", "t", "--stage", "synth", "--dry-run", "--", "true")
    assert code == 1 and "not a git repository" in err and err.rstrip().endswith("; pass --source")


def test_track_execs_the_driver_and_the_watcher_collects(demo: Path) -> None:
    p = track("--label", "t", "--stage", "synth", "--source", "abc1234", "--build-tag", "bt_a", "--collect", "--", *FLOW)
    assert p.returncode == 0, p.stderr
    project = config.load_project(demo)
    with Database(project.data / "edr.db") as db:
        (row,) = db.runs(batch="track")
        assert (row["build_tag"], row["config"]) == ("bt_a", "t")
        hb = json.loads((project.state_dir / "track" / f"{row['run_id']}.json").read_text())
        host = socket.gethostname()
        assert (hb["phase"], hb["host"], hb["stage"]) == ("done", host, "synth")
        assert row["handle"] == f"track:{host}:{hb['driver_pid']}" and row["root"] == str(demo) and row["cores"] == 1
        watch.cycle(project, Ssh(project.site), db, [])
        assert db.run(row["run_id"])["state"] == "done"
        assert (project.data / "results" / row["run_id"] / "reports" / "3" / "area.rpt").is_file()
        assert {m["name"] for m in db.metrics(run_ids=[row["run_id"]])} >= {"area_cell_um2"}


def test_track_passes_the_driver_exit_code_and_collects_only_on_request(demo: Path) -> None:
    p = track("--label", "f", "--stage", "check", "--source", "abc1234", "--", "sh", "-c", "echo no; exit 3")
    assert p.returncode == 5, p.stderr
    project = config.load_project(demo)
    with Database(project.data / "edr.db") as db:
        (row,) = db.runs(batch="track")
        hb = json.loads((project.state_dir / "track" / f"{row['run_id']}.json").read_text())
        assert hb["phase"] == "FAILED:check" and (demo / "log" / "check.log").read_text().endswith("no\n")
        watch.cycle(project, Ssh(project.site), db, [])
        assert db.run(row["run_id"])["state"] == "failed" and not (project.data / "results" / row["run_id"]).exists()


def test_help_names_the_json_form_and_says_retire_keeps_the_logs(capsys) -> None:
    for name in ("status", "plan", "launch", "retire"):
        assert cli.main([name, "--help"]) == 0
        assert f"the same as edr --json {name}" in capsys.readouterr().out
    cli.main(["retire", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "copied log/ and the collect paths of every finished stage to data/results/<run id>/" in out

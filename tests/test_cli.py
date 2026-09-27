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
import subprocess
import sys
import time
from pathlib import Path

import pytest

from edarunner import board, cli, config, launch, watch
from edarunner import db as db_mod
from edarunner.db import Database
from edarunner.guards import Refuse
from edarunner.hosts import HostError, HostProbe, Ssh
from edarunner.model import Telegram
from edarunner.notify import Notifier
from edarunner.notify.telegram import TelegramBot

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
DATE = "20260926_1200"


@pytest.fixture
def demo(tmp_path: Path, monkeypatch) -> Path:
    """The demo copied under tmp_path/edr (the safety marker), its scratch and HOME under tmp_path, cwd in the copy."""
    root = tmp_path / "edr" / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    (tmp_path / "scratch").mkdir()
    site = root / "site.toml"
    site.write_text(site.read_text().replace('"/tmp/edr-demo"', f'"{tmp_path / "scratch"}"'))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("EDR_BATCH", raising=False)
    monkeypatch.chdir(root)
    return root


def edr(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def dead_pid() -> int:
    p = subprocess.Popen(["sleep", "0"])
    p.wait()
    return p.pid


def bdir(root: Path, batch: str = "demo") -> Path:
    """The batch directory of the state: <HOME>/.edr/<project>/<batch>."""
    return Path.home() / ".edr" / "demo" / batch


def seed(root: Path, label: str, phase: str | None, pid: int | None = None, batch: str = "demo",
         src: str = "abc1234", date: str = DATE, tree: bool = True, **extra) -> str:
    """A database row, a heartbeat and a run tree under the tmp scratch; returns the run id."""
    run_id = f"{date}_{label}_demo_g{src}"
    project = config.load_project(root)
    now = int(time.time())
    row = {"run_id": run_id, "batch": batch, "label": label, "config": "demo", "src": src, "host": "local",
           "phase": phase, "state": "running", "started": now - 100, "updated": now - 5,
           "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 0, "queued": 0}, **extra}
    if tree:
        run_root = Path(project.site.scratch[0]) / getpass.getuser() / "edr" / "demo" / run_id
        (run_root / "out" / "11").mkdir(parents=True)
        (run_root / "out" / "11" / "netlist.v").write_text("module top; endmodule\n")
        (run_root / "log").mkdir()
        row["root"] = str(run_root)
        terminal = str(phase).startswith(board.TERMINAL)
        hb = {**row, "driver_pid": pid, "pgids": [], "stage": "synth", "step": 2, "step_name": "elaborate",
              "tasks": {}, "exit": 0 if terminal else None, "last_log": "step 2 elaborate"}
        (project.state_dir / batch).mkdir(parents=True, exist_ok=True)
        (project.state_dir / batch / f"{run_id}.json").write_text(json.dumps(hb))
    with Database(project.data / "edr.db") as db:
        db.upsert_batch({"batch": batch, "project": "demo", "source": src})
        db.upsert_run(row)
    return run_id


def with_vars(root: Path, run_id: str) -> None:
    """The spec of a seeded run with the job vars of the demo job `a`."""
    (bdir(root) / f"{run_id}.spec.json").write_text(json.dumps({"vars": {"netlist_stage": "11"}}))


def add_metric(root: Path, run_id: str, name: str, value: float, step: int | None = 3, task: str = "") -> None:
    with Database(config.load_project(root).data / "edr.db") as db:
        db.add_metric({"run_id": run_id, "stage": "synth", "step": step, "task": task, "name": name,
                        "canonical": "design__instance__area" if name == "area_cell_um2" else "", "value": value, "unit": "u"})


def keep_file(root: Path, run_id: str) -> dict:
    return json.loads((bdir(root) / f"{run_id}.keep.json").read_text())


# init and check


def test_init_writes_edr_toml_once(tmp_path: Path, monkeypatch, capsys) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    code, out, _ = edr(capsys, "init", "--site", str(DEMO))
    assert code == 0 and "edr.toml" in out
    text = (fresh / "edr.toml").read_text()
    assert f'site = "{DEMO / "site.toml"}"' in text and 'project = "fresh"' in text and "$project" not in text
    unit = (fresh / "edr-watch.service").read_text()
    assert f"WorkingDirectory={fresh}" in unit and "Restart=always" in unit and " watch\n" in unit
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
    assert code == 0 and f"edr export --design abc1234" in out and f"edr launch demo --only q" in out
    assert "b_nodw@demo" not in out  # fresh heartbeat: running, nothing to triage
    code, out, _ = edr(capsys, "status", "--triage", "--live")
    assert code == 0 and f"edr continue b_nodw@demo --stage synth --from elaborate" in out
    code, out, _ = edr(capsys, "status", "--batch", "other")
    assert code == 0 and out == "no runs\n"
    code, _, err = edr(capsys, "status", "nope@demo")
    assert code == 1 and "nope" in err
    code, _, err = edr(capsys, "status", "#9")
    assert code == 1


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
    assert "not been probed yet" in out and not (demo / "data" / "edr.db").exists()


def test_brief_has_its_sections_in_order(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    seed(demo, "b", "FAILED:synth", exit=5)
    seed(demo, "c", "stage:synth")
    (demo / "wt" / "abc1234").mkdir(parents=True)
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("user", a, "launch", "local /x")
        db.add_host_samples(int(time.time()) - 60, {"local": {"cores": 4, "load": 3.8, "total_ram_gb": 8,
                                                              "free_ram_gb": 6, "total_gb": 100, "free_gb": 90}})
    (demo / "AGENTS.md").write_text("notes\n")
    code, out, _ = edr(capsys, "brief")
    heads = [ln for ln in out.splitlines() if ln.startswith("## ")]
    assert code == 0 and heads == ["## The flow", "## The site", "## The state", "## Read more"]
    assert "- `synth` runs one command through 4 steps, collects `reports/` and needs the tool `demo`." in out
    assert "- `power` is a task group that runs 2 tasks at a time" in out
    assert "`local` at the probe 60 seconds ago: cores 🔴, ram 🟢, scratch 🟢." in out
    assert "Batch `demo` on source `abc1234` has 3 runs: 1 failed, 1 running and 1 done." in out
    assert "One source is checked out under" in out and ": `abc1234` (batch `demo`)." in out
    assert "One run has not finished:" in out and "`c@demo` is running in stage `synth` at step 2 (elaborate) on `local`" in out
    assert "`b@demo` (failed): `edr retire b@demo --why FAILED:synth`" in out
    assert "user recorded `launch` on `a@demo`: local /x" in out and f"`{demo / 'AGENTS.md'}`" in out
    code, out, _ = edr(capsys, "brief", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and {"project", "root", "repo", "sources", "backend", "stages", "hosts", "tools", "batches",
                          "live", "decisions", "events", "read", "docs"} <= data.keys()
    assert data["hosts"][0]["marks"]["cores"] == "🔴" and [d["handle"] for d in data["decisions"]] == ["b@demo", "a@demo"]


def test_brief_proposes_nothing_for_a_retired_run(demo: Path, capsys) -> None:
    seed(demo, "b", "FAILED:synth", exit=5)
    seed(demo, "c", "stage:synth", pid=dead_pid(), updated=int(time.time()) - 3600)
    assert edr(capsys, "retire", "b@demo", "--uncollected", "--why", "failed")[0] == 0
    assert edr(capsys, "retire", "c@demo", "--uncollected", "--why", "gone")[0] == 0
    code, out, _ = edr(capsys, "brief", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["decisions"] == [] and data["batches"][0]["states"] == {"retired": 2}

def test_brief_run_tells_a_failed_run_with_its_command(demo: Path, capsys) -> None:
    b = seed(demo, "b", "FAILED:synth", exit=5, stage="synth", step=2)
    log = Path(json.loads((bdir(demo) / f"{b}.json").read_text())["root"]) / "log" / "synth.log"
    log.write_text("".join(f"line {i}\n" for i in range(30)) + "Error: no licence\n")
    now = int(time.time())
    hb = json.loads((bdir(demo) / f"{b}.json").read_text())
    (bdir(demo) / f"{b}.json").write_text(json.dumps({
        **hb, "exit": 5, "log": str(log), "step_times": {"synth": {"1": now - 90, "2": now - 50}},
        "stages": {"synth": {"attempt": 1, "status": "failed", "started": now - 90, "ended": now - 10, "exit": 5}}}))
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("watch", b, "failed", "synth ended FAILED")
    add_metric(demo, b, "area_cell_um2", 12.5)
    code, out, _ = edr(capsys, "brief", "--run", "b@demo")
    assert code == 0 and out.startswith("# b@demo\n") and f"`{b}`" in out and "ended with the phase `FAILED:synth` and exit 5, so its state is failed." in out
    assert "- Stage `synth`, attempt 1," in out and "ran for 1m, ending failed with exit 5." in out
    assert "  - Step 2 (elaborate) started" in out and "watch recorded `failed` on `b@demo`: synth ended FAILED" in out
    assert "Error: no licence" in out and "line 11\n" in out and "line 10\n" not in out
    assert "`design__instance__area` is 12.5 u at `synth` step 3." in out
    assert "The triage proposes `edr retire b@demo --why FAILED:synth`." in out and "the run ended `FAILED`" in out
    code, out, _ = edr(capsys, "--json", "brief", "--run", "b@demo")
    data = json.loads(out)["data"]
    assert code == 0 and data["command"] == "edr retire b@demo --why FAILED:synth" and data["state"] == "failed"
    assert {"runtime", "events", "log_tail", "metrics", "reason"} <= data.keys()
    code, _, err = edr(capsys, "brief", "--run", "nope@demo")
    assert code == 1 and "nope" in err


# keep, stop, actions


def test_keep_merges_fields(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    assert edr(capsys, "keep", "b@demo")[0] == 1
    code, out, _ = edr(capsys, "keep", "b_nodw@demo", "--hours", "3", "--dry-run")
    assert code == 0 and "(dry)" in out and not list(bdir(demo).glob("*.keep.json"))
    assert edr(capsys, "keep", "b_nodw@demo", "--hours", "3")[0] == 0 and keep_file(demo, b) == {"hours": 3, "ack": False}
    assert edr(capsys, "keep", "b_nodw@demo", "--ack")[0] == 0 and keep_file(demo, b) == {"hours": 3, "ack": True}
    assert edr(capsys, "keep", "b_nodw@demo")[0] == 0 and keep_file(demo, b) == {"hours": 12, "ack": True}
    code, out, _ = edr(capsys, "keep", "a@demo")
    assert code == 2 and "already done" in out
    with Database(demo / "data" / "edr.db") as db:
        assert [(e["actor"], e["kind"], e["text"]) for e in db.events()] == [
            ("user", "keep", "keep 3 h"), ("user", "keep", "keep 3 h, ack"), ("user", "keep", "keep 12 h, ack")]


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
    add_metric(demo, a, "area_cell_um2", 1031.5, step=4)
    add_metric(demo, b, "area_cell_um2", 999.0)
    add_metric(demo, a, "power_w", 0.25, step=None, task="k_small")
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert acts.keep("b_nodw@demo", 5, "telegram") == "b_nodw@demo: keep 5 h" and keep_file(demo, b) == {"hours": 5, "ack": False}
    assert acts.ack("b_nodw@demo", "telegram") == "b_nodw@demo: keep 5 h, ack" and keep_file(demo, b)["ack"] is True
    assert acts.stop_after_task("b_nodw@demo", "telegram", "why") == "b_nodw@demo stops after its task"
    assert acts.stop_after_task("a@demo", "telegram", "why") == "a@demo already done"
    assert (bdir(demo) / f"{b}.stop").exists()
    with Database(demo / "data" / "edr.db") as db:
        assert {e["actor"] for e in db.events()} == {"telegram"} and len(db.events()) == 3
    assert acts.status_text().splitlines() == ["🟢 <code>b_nodw@demo</code> synth 2/4, 0m", "⚪ <code>a@demo</code> done, 0m",
                                               "<i>1 running, 1 done</i>"]
    with Database(demo / "data" / "edr.db") as db:
        assert len(db.get_store("last_board")) == 2
    one = acts.status_text("b_nodw@demo").splitlines()
    assert one == ["🟢 <code>b_nodw@demo</code> running", "stage synth, step 2 elaborate", "on local, 0m",
                   "<pre>step 2 elaborate</pre>"]
    assert acts.status_text("a@demo").splitlines()[3] .startswith("<code>edr export --design ")
    events = acts.events_text(2).splitlines()
    assert events[0][5:] == " <b>stop</b> <code>b_nodw@demo</code>" and events[1] == "    <i>after-task: why</i>"
    assert len(events) == 4
    assert b not in acts.events_text(8)
    cmp = acts.compare_text(["a@demo", "b_nodw@demo"]).splitlines()
    assert cmp[:3] == ["design__instance__area", "  a       1031.5", "  b_nodw   999.0"] and cmp[5].split() == ["b_nodw", "-"]
    assert len(acts.metric_text("design__instance__area", None).splitlines()) == 4 and acts.metric_text("design__instance__area", "zzz") == "no metrics"
    assert acts.hosts_text().startswith("<b>local</b> ")
    assert acts.tools_text() == "<b>demo</b> 2/10 seats used, local"
    assert acts.metrics_csv("abc1234").decode().splitlines()[0].startswith("run_id,label,")
    assert len(acts.metrics_csv("abc1234").decode().splitlines()) == 5 and acts.metrics_csv("zzz").count(b"\n") == 1
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
    capsys.readouterr()



def test_a_button_press_records_one_event(demo: Path, tmp_path: Path) -> None:
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    token = tmp_path / "token"
    token.write_text("1:A")
    ctx = cli.Ctx(argparse.Namespace(json=False, dry_run=False))
    ctx.project.site.telegram = Telegram(token_file=token, chat_id=42)
    bot = TelegramBot(ctx.project.site, ctx.project, ctx.db, cli.Actions(ctx), str(token))
    bot.api.call = lambda method, params, files=None: {}
    press = {"id": "q", "from": {"id": 7}, "data": "ack:b_nodw@demo",
             "message": {"message_id": 1, "chat": {"id": 42}, "text": "dead b_nodw@demo"}}
    bot.handle_update({"update_id": 1, "callback_query": press})
    events = ctx.db.events()
    assert [(e["actor"], e["run_id"], e["kind"]) for e in events] == [("telegram", b, "keep")]
    ctx.close()

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
    monkeypatch.setattr(watch, "orphans", lambda *a: [])  # the sleeps of other tests on this machine
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
    code, out, _ = edr(capsys, "metrics", "--design", "abc1234", "--csv")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("run_id,label,config,design,stage,step,task,metric") and len(lines) == 3
    assert lines[1].startswith(f"{a},a,demo,abc1234,synth,3,,area_cell_um2,design__instance__area,1000.0,u,")
    code, out, _ = edr(capsys, "metrics", "--design", "abc1234", "--stage", "pnr")
    assert code == 2 and out == "no metrics\n"
    code, out, _ = edr(capsys, "metrics", "--design", "abc1234", "--step", "3")
    assert code == 0 and "wns_ns" in out
    assert edr(capsys, "metrics", "--design", "abc")[0] == 2  # exact match, not a prefix
    exp = tmp_path / "exp"
    code, out, _ = edr(capsys, "export", "--design", "abc1234", "--out", str(exp), "--dry-run")
    assert code == 0 and not exp.exists() and "reports/3/area.rpt" in out
    code, out, _ = edr(capsys, "--json", "export", "--design", "abc1234", "--out", str(exp))
    manifest = json.loads(out)["data"]
    assert code == 0 and manifest == json.loads((exp / "manifest.json").read_text()) and (exp / "a" / "reports" / "3" / "area.rpt").exists()
    code, _, err = edr(capsys, "export", "--design", "abc1234", "--out", str(exp))
    assert code == 1 and "not empty" in err
    with Database(demo / "data" / "edr.db") as db:
        assert [e["kind"] for e in db.events()] == ["export"]


def test_extract_replaces_changed_rows(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    add_metric(demo, a, "area_cell_um2", 1000.0)  # unit u, the config says um2
    reports = demo / "data" / "results" / a / "reports" / "3"
    reports.mkdir(parents=True)
    (reports / "area.rpt").write_text("i_top 1000.0\n")
    (reports / "qor.rpt").write_text("Critical Path Slack: -0.25\n")
    assert edr(capsys, "extract")[0] == 1 and edr(capsys, "extract", "a@demo", "--batch", "demo")[0] == 1
    code, out, _ = edr(capsys, "extract", "a@demo", "--dry-run")
    assert code == 0 and out == f"{a}: 1 new, 1 changed, 0 unchanged, 0 failed (dry)\n"
    with Database(demo / "data" / "edr.db") as db:
        assert [m["unit"] for m in db.metrics(run_ids=[a])] == ["u"] and db.events() == []
    code, out, _ = edr(capsys, "--json", "extract", "--design", "abc1234")
    assert code == 0 and json.loads(out)["data"] == [{"run_id": a, "new": 1, "changed": 1, "unchanged": 0, "failed": 0}]
    with Database(demo / "data" / "edr.db") as db:
        assert {(m["name"], m["value"], m["unit"]) for m in db.metrics(run_ids=[a])} == {
            ("area_cell_um2", 1000.0, "um2"), ("wns_ns", -0.25, "ns")}
        assert [(e["kind"], e["text"]) for e in db.events()] == [("extract", "1 new, 1 changed, 0 unchanged, 0 failed")]
    code, out, _ = edr(capsys, "extract", "--batch", "demo")
    assert code == 0 and out == f"{a}: 0 new, 0 changed, 2 unchanged, 0 failed\n"
    assert edr(capsys, "extract", "--design", "0000000")[0] == 2


def test_hosts_and_tools_probe_local(demo: Path, capsys, tmp_path: Path) -> None:
    code, out, _ = edr(capsys, "hosts")
    head, row = out.splitlines()[0], out.splitlines()[2]
    assert code == 0 and head.split() == ["ok", "host", "cores", "load", "ram", "GB", "mount", "scratch", "GB", "gpu", "gpu", "GB", "tools", "runs"]
    assert row.split()[1] == "local" and str(tmp_path / "scratch") in row and re.search(r" \d+/\d+ +[\u2588\u2591]{8} ", row)
    code, out, _ = edr(capsys, "--json", "hosts", "--narrow")
    assert code == 0 and json.loads(out)["data"][0]["host"] == "local"
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


def test_hosts_table_from_fake_probes(demo: Path, capsys, monkeypatch) -> None:
    site = demo / "site.toml"
    site.write_text(site.read_text() + "\n[hosts.hostA]\ncores = 64\nram_gb = 256\n\n[hosts.hostB]\ncores = 32\nram_gb = 128\n")
    probes = {
        "local": HostProbe("local", 7.8, 35.0, "/tmp/x", 15.5, 3, 0, 0, cores=8, load=0.2, total_ram_gb=62.3, total_gb=15.6),
        "hostA": HostProbe("hostA", 12.5, 120.0, "/scratch", 800.0, 4, 2, 3, cores=64, load=51.5, total_ram_gb=256.0,
                           total_gb=2000.0, gpus=4, gpus_idle=1, gpu_used_gb=30.0, gpu_total_gb=320.0),
    }

    def probe(self, host):
        if host in probes:
            return probes[host]
        raise HostError(f"{host}: rc 255: timeout")

    monkeypatch.setattr(Ssh, "probe", probe)
    code, out, _ = edr(capsys, "hosts")
    lines = out.splitlines()
    assert code == 3 and len(lines) == 5 and "\x1b" not in out
    assert [ln.split()[:2] for ln in lines[2:]] == [["⚫", "hostB"], ["🟠", "hostA"], ["🟢", "local"]]
    a = next(ln for ln in lines if " hostA " in ln)
    assert a.split() == ["🟠", "hostA", "🟠", "52/64", "\u2588" * 6 + "\u2591" * 2, "51.5", "🟢", "120/256", "/scratch", "🟢",
                         "800/2000", "\u2588" * 5 + "\u2591" * 3, "🟡", "1/4", "290/320", "4/2", "3"]
    local = next(ln for ln in lines if " local " in ln)
    assert local.split() == ["🟢", "local", "🟢", "0/8", "\u2591" * 8, "0.2", "🟢", "35/62.3", "/tmp/x", "🟢", "15.5/15.6",
                             "\u2591" * 8, "-", "-", "3/0", "0"]
    assert next(ln for ln in lines if " hostB " in ln).split()[2:] == ["error:", "hostB:", "rc", "255:", "timeout"]
    code, out, _ = edr(capsys, "hosts", "--narrow")
    lines = out.splitlines()
    assert code == 3 and all(len(ln) <= 48 for ln in lines)
    assert lines[0].split() == ["ok", "host", "cores", "ram", "GB", "scratch", "GB", "gpu"]
    assert next(ln for ln in lines if " hostA " in ln).split() == ["🟠", "hostA", "🟠52/64", "🟢120/256", "🟢800/2000", "🟡1/4"]
    code, out, _ = edr(capsys, "--json", "hosts")
    data = json.loads(out)["data"]
    assert [r["host"] for r in data] == ["hostB", "hostA", "local"]
    data = {r["host"]: r for r in data}
    assert code == 3 and data["hostA"]["gpus_idle"] == 1 and data["hostA"]["total_gb"] == 2000.0 and "error" in data["hostB"]
    assert data["hostA"]["marks"] == {"cores": "🟠", "ram": "🟢", "scratch": "🟢", "gpu": "🟡"}
    assert data["local"]["marks"]["gpu"] == "-" and set(data["hostB"]["marks"].values()) == {"⚫"}
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    text = acts.hosts_text().splitlines()
    assert ("<b>hostA</b> 🟠 cores 52/64, 🟢 ram 136/256 GB, 🟢 scratch 1200/2000 GB, 🟡 gpu 3/4" in text
            and "⚫ <b>hostB</b> <i>no answer</i>" in text and text[0].startswith("⚫"))


def test_hosts_sort_red_first_by_marks(demo: Path, capsys, monkeypatch) -> None:
    site = demo / "site.toml"
    site.write_text(site.read_text() + "".join(f"\n[hosts.{h}]\ncores = 8\nram_gb = 8\n" for h in ("b", "a", "c")))
    (demo / "edr.toml").write_text((demo / "edr.toml").read_text() + "\n[marks]\nram = [0.1, 0.2, 0.3]\n")
    ram_used = {"local": 0.0, "a": 0.25, "b": 0.3, "c": 0.3}

    def probe(self, host):
        return HostProbe(host, 8, 10 - 10 * ram_used[host], "/s", 10, cores=8, total_ram_gb=10, total_gb=10)

    monkeypatch.setattr(Ssh, "probe", probe)
    code, out, _ = edr(capsys, "--json", "hosts")
    data = json.loads(out)["data"]
    assert code == 0 and [(r["host"], r["marks"]["ram"]) for r in data] == [("b", "🔴"), ("c", "🔴"), ("a", "🟠"), ("local", "🟢")]


def test_continue_reuse_dry_and_collect(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    with_vars(demo, a)
    assert edr(capsys, "continue", "a@demo")[0] == 1
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
    argv = ["import", "--run-id", root.name, "--label", "ref", "--config", "demo", "--src", "abc1234",
            "--host", "local", "--root", str(root), "--why", "reference"]
    code, out, _ = edr(capsys, *argv, "--dry-run")
    assert code == 0 and "(dry)" in out and not (demo / "data" / "edr.db").exists()
    assert edr(capsys, *argv)[0] == 0
    with Database(demo / "data" / "edr.db") as db:
        row = db.run(root.name)
        assert row["phase"] == "done" and row["state"] == "imported" and row["root"] == str(root)
        assert [b["batch"] for b in db.batches()] == ["imported"]
        assert db.events()[-1]["kind"] == "import"
    assert edr(capsys, "import", "--run-id", "bad", "--label", "r", "--config", "demo", "--src", "a",
               "--host", "local", "--root", str(root))[0] == 1
    assert edr(capsys, "import", "--run-id", root.name, "--label", "r", "--config", "demo", "--src", "a",
               "--host", "local", "--root", str(root / "missing"))[0] == 1
    other = root.with_name("20260904_0412_noconf_x_gabc1234")
    other.mkdir()
    assert edr(capsys, "import", "--run-id", other.name, "--label", "noconf", "--src", "a", "--host", "local",
               "--root", str(other))[0] == 0  # a job's config is optional, so it is here too


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
    assert edr(capsys, "import", "--run-id", root.name, "--label", "ref", "--config", "demo", "--src", "abc1234",
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
                        "host": row["host"], "root": row["root"], "src": "abc1234", "phase": "done", "state": "done"})
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


@pytest.mark.parametrize("old,new", [("lic", "tools"), ("stage", "checkout"), ("run", "continue")])
def test_a_removed_command_names_its_replacement(demo: Path, capsys, old: str, new: str) -> None:
    code, out, err = edr(capsys, "--json", old, "x")
    assert code == 1 and out == "" and err == f"edr: {old} was removed in 0.4.0; use edr {new}\n"


def test_retire_batch_removes_the_checked_out_tree_no_other_batch_uses(demo: Path, capsys) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=demo, check=True, capture_output=True)
    repo = demo / "repo"
    src = json.loads(edr(capsys, "--json", "checkout", "HEAD")[1])["data"]["src"]
    wt = demo / "wt" / src
    seed(demo, "a", "done", src=src)
    seed(demo, "b", "done", src=src, batch="other")
    assert edr(capsys, "retire", "--batch", "other", "--uncollected", "--why", "x")[0] == 0
    assert (wt / ".git").is_file()  # demo still has the source
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "x", "--dry-run")
    assert code == 0 and f"git worktree remove --force {wt} (dry)" in out and (wt / ".git").is_file()
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "x")
    assert code == 0 and not wt.exists() and repo.is_dir()
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True, check=True).stdout
    assert str(wt) not in listed
    with open(repo / "flow" / "flow.sh", "a") as f:
        f.write("# dirty\n")
    dirty = json.loads(edr(capsys, "--json", "checkout", "--dirty", str(repo))[1])["data"]["src"]
    snap = demo / "wt" / dirty
    assert "-dirty-" in dirty and snap.is_dir() and not (snap / ".git").exists()
    seed(demo, "c", "done", src=dirty, batch="snap")
    code, out, _ = edr(capsys, "retire", "--batch", "snap", "--uncollected", "--why", "y")
    assert code == 0 and f"rm -rf {snap}" in out and not snap.exists() and (repo / "flow").is_dir()
    with Database(demo / "data" / "edr.db") as db:
        assert [e["text"] for e in db.events() if e["run_id"] == ""] == [f"x: worktree {wt}", f"y: worktree {snap}"]


def test_retire_batch_keeps_a_worktree_without_the_marker(demo: Path, capsys, tmp_path: Path) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=demo, check=True, capture_output=True)
    toml = demo / "edr.toml"
    toml.write_text(toml.read_text().replace('worktrees = "wt"', f'worktrees = "{tmp_path / "rtl-wt"}"'))
    src = json.loads(edr(capsys, "--json", "checkout", "HEAD")[1])["data"]["src"]
    wt = tmp_path / "rtl-wt" / src
    a = seed(demo, "a", "done", src=src)
    with Database(demo / "data" / "edr.db") as db:
        root = Path(db.run(a)["root"])
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--uncollected", "--why", "x")
    assert code == 0 and f"demo: worktree kept: '{wt}' does not contain the marker '/edr/'; " \
        "remove it with git worktree remove" in out
    assert (wt / ".git").is_file() and not root.exists() and f"rm -rf {root}" in out
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
    base = ["import", "--run-id", run_id, "--label", "ref", "--config", "demo", "--src", "abc1234"]
    assert edr(capsys, *base)[0] == 1  # neither a tree nor results
    assert edr(capsys, *base, "--results", str(src), "--tasks", "nope")[0] == 1
    code, out, _ = edr(capsys, *base, "--results", str(src), "--tasks", "k_small", "--dry-run")
    assert code == 0 and "(dry)" in out and not (demo / "data").exists()
    code, out, _ = edr(capsys, *base, "--results", str(src), "--tasks", "k_small")
    link = demo / "data" / "results" / run_id
    assert code == 0 and "4 metrics" in out and link.is_symlink() and link.resolve() == src.resolve()
    code, out, _ = edr(capsys, "metrics", "--design", "abc1234", "--csv")
    got = {ln.split(",")[7]: ln.split(",")[9] for ln in out.splitlines()[1:]}
    assert code == 0 and got == {"area_cell_um2": "1000.0", "power_w": "0.25", "window_ns": "3400.0", "energy_nj": "850.0"}
    with Database(demo / "data" / "edr.db") as db:
        row = db.run(run_id)
        assert row["root"] is None and row["host"] == "" and row["state"] == "imported"
        assert db.events()[-1]["text"].endswith("4 metrics")
    other = tmp_path / "other"
    other.mkdir()
    assert edr(capsys, *base, "--results", str(other))[0] == 1  # never replaces a linked tree
    assert edr(capsys, "continue", "ref@imported", "--stage", "power", "--tasks", "k_small", "--on", "local", "--dry-run")[0] == 1
    exp = tmp_path / "exp"
    code, out, _ = edr(capsys, "export", "--design", "abc1234", "--out", str(exp))
    assert code == 0 and (exp / "ref" / "reports" / "3" / "area.rpt").is_file()


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


def test_status_digest_prints_the_digest_as_text(demo: Path, capsys) -> None:
    seed(demo, "a", "done")
    code, out, _ = edr(capsys, "status", "--digest")
    assert code == 0 and out.startswith("Ended since ") and "⚪ a@demo done" in out and "<" not in out
    code, out, _ = edr(capsys, "--json", "status", "--digest")
    assert code == 0 and "<code>a@demo</code>" in json.loads(out)["data"]["digest"]


# track

FLOW = ["bash", "flow/flow.sh", "synth", "x", "demo", "LAST_STAGE=synth"]


def track(*argv: str) -> subprocess.CompletedProcess:
    """edr track in its own process, so its exec replaces that process and not the test."""
    return subprocess.run([sys.executable, "-m", "edarunner.cli", "track", *argv], capture_output=True, text=True,
                          timeout=120)


def test_track_dry_run_prints_the_spec_and_writes_nothing(demo: Path, capsys) -> None:
    code, out, _ = edr(capsys, "track", "--label", "t", "--stage", "synth", "--src", "abc1234", "--dry-run", "--", *FLOW)
    spec = json.loads(out[out.index("{"):])
    (st,) = spec["stages"]
    assert code == 0 and st["cmd"] == shlex.join(FLOW) and "resume" not in st and st["steps"][0] == "setup"
    assert spec["batch"] == "track" and spec["host"] is None and spec["root"] == str(demo) and spec["collect"] is False
    assert spec["run_id"].endswith("_t_track_gabc1234") and spec["start_at"] == {"stage": "synth", "checkpoint": None}
    assert not (Path.home() / ".edr").exists() and not (demo / "data" / "edr.db").exists()
    code, _, err = edr(capsys, "track", "--label", "t", "--stage", "power", "--src", "a", "--dry-run", "--", "true")
    assert code == 1 and "task group" in err
    code, _, err = edr(capsys, "track", "--label", "t", "--stage", "synth", "--dry-run", "--", "true")
    assert code == 1 and "not a git tree; pass --src" in err


def test_track_execs_the_driver_and_the_watcher_collects(demo: Path) -> None:
    p = track("--label", "t", "--stage", "synth", "--src", "abc1234", "--collect", "--", *FLOW)
    assert p.returncode == 0, p.stderr
    project = config.load_project(demo)
    with Database(project.data / "edr.db") as db:
        (row,) = db.runs(batch="track")
        hb = json.loads((project.state_dir / "track" / f"{row['run_id']}.json").read_text())
        host = socket.gethostname()
        assert (hb["phase"], hb["host"], hb["stage"]) == ("done", host, "synth")
        assert row["handle"] == f"track:{host}:{hb['driver_pid']}" and row["root"] == str(demo)
        watch.cycle(project, Ssh(project.site), db, [])
        assert db.run(row["run_id"])["state"] == "done"
        assert (project.data / "results" / row["run_id"] / "reports" / "3" / "area.rpt").is_file()
        assert {m["name"] for m in db.metrics(run_ids=[row["run_id"]])} >= {"area_cell_um2"}


def test_track_passes_the_driver_exit_code_and_collects_only_on_request(demo: Path) -> None:
    p = track("--label", "f", "--stage", "check", "--src", "abc1234", "--", "sh", "-c", "echo no; exit 3")
    assert p.returncode == 5, p.stderr
    project = config.load_project(demo)
    with Database(project.data / "edr.db") as db:
        (row,) = db.runs(batch="track")
        hb = json.loads((project.state_dir / "track" / f"{row['run_id']}.json").read_text())
        assert hb["phase"] == "FAILED:check" and (demo / "log" / "check.log").read_text().endswith("no\n")
        watch.cycle(project, Ssh(project.site), db, [])
        assert db.run(row["run_id"])["state"] == "failed" and not (project.data / "results" / row["run_id"]).exists()

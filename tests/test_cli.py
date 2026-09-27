"""cli.py: every verb on a copy of examples/local-demo in tmp_path, host local, no driver started."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from edarunner import board, cli, config
from edarunner.ledger import Ledger

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
DATE = "20260926_1200"


@pytest.fixture
def demo(tmp_path: Path, monkeypatch) -> Path:
    """The demo copied to tmp_path, its scratch and HOME under tmp_path, cwd in the copy."""
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    (tmp_path / "scratch").mkdir()
    site = root / "site.toml"
    site.write_text(site.read_text().replace('"/tmp/edr-demo"', f'"{tmp_path / "scratch"}"'))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("EDR_BATCH", raising=False)
    monkeypatch.chdir(root)
    monkeypatch.setattr(board, "ensure_plotly", lambda d: None)
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
    return root.parent / ".edr" / "demo" / batch


def seed(root: Path, label: str, phase: str | None, pid: int | None = None, batch: str = "demo",
         src: str = "abc1234", date: str = DATE, tree: bool = True, **extra) -> str:
    """A ledger row, a heartbeat and a run tree under the tmp scratch; returns the run id."""
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
        (project.state / batch).mkdir(parents=True, exist_ok=True)
        (project.state / batch / f"{run_id}.json").write_text(json.dumps(hb))
    with Ledger(project.data / "edr.db") as led:
        led.upsert_batch({"batch": batch, "project": "demo", "source": src})
        led.upsert_run(row)
    return run_id


def add_metric(root: Path, run_id: str, name: str, value: float, step: int | None = 3, task: str = "") -> None:
    with Ledger(config.load_project(root).data / "edr.db") as led:
        led.add_metric({"run_id": run_id, "stage": "synth", "step": step, "task": task, "name": name,
                        "canonical": "area.cell" if name == "area_cell_um2" else "", "value": value, "unit": "u"})


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
    assert code == 1 and "demo/a: missing placeholder {nope}" in out


# status, events, handles


def test_status_boards_handles_live_and_triage(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    q = seed(demo, "q", None, tree=False, state="queued")
    code, out, _ = edr(capsys, "status")
    assert code == 0 and "b_nodw" in out and "done" in out and out.startswith("#")
    last = json.loads((demo / "data" / "board" / "last_board.json").read_text())
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
    assert code == 0 and f"edr run b_nodw@demo --stage synth --from elaborate" in out
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
    with Ledger(demo / "data" / "edr.db") as led:
        led.add_event("user", a, "launch", "local /x")
        led.add_event("watch", a, "done", "")
        led.add_event("user", "", "export", "x -> y")
    code, out, _ = edr(capsys, "events", "-n", "2")
    assert code == 0 and out.count("\n") == 2 and "launch" not in out and "export" in out
    code, out, _ = edr(capsys, "events", "--run", "a@demo")
    assert code == 0 and out.count("\n") == 2 and "a@demo launch: local /x" in out
    code, out, _ = edr(capsys, "--json", "events", "--since", "1h")
    assert code == 0 and len(json.loads(out)["data"]) == 3
    code, out, _ = edr(capsys, "events", "--since", "1")
    assert code in (0, 2)
    code, _, err = edr(capsys, "events", "--since", "soon")
    assert code == 1 and "--since" in err


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
    with Ledger(demo / "data" / "edr.db") as led:
        assert [(e["actor"], e["kind"], e["text"]) for e in led.events()] == [
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
    with Ledger(demo / "data" / "edr.db") as led:
        texts = [e["text"] for e in led.events(run_id=b)]
    assert len(texts) == 2 and texts[0] == "after-task: later" and texts[1].startswith("gone [driver")


def test_actions_for_the_bot(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    add_metric(demo, a, "area_cell_um2", 1000.0)
    add_metric(demo, a, "area_cell_um2", 1031.5, step=4)
    add_metric(demo, b, "area_cell_um2", 999.0)
    add_metric(demo, a, "power_w", 0.25, step=None, task="k_small")
    acts = cli.Actions(cli.Ctx(argparse.Namespace(json=False, dry_run=False)))
    assert acts.keep("b_nodw@demo", 5, "telegram") == f"{b}: keep 5 h" and keep_file(demo, b) == {"hours": 5, "ack": False}
    assert acts.ack("b_nodw@demo", "telegram") == f"{b}: keep 5 h, ack" and keep_file(demo, b)["ack"] is True
    assert acts.stop_after_task("b_nodw@demo", "telegram", "why") == f"{b} stops after its task"
    assert acts.stop_after_task("a@demo", "telegram", "why") == f"{a} already done"
    assert (bdir(demo) / f"{b}.stop").exists()
    with Ledger(demo / "data" / "edr.db") as led:
        assert {e["actor"] for e in led.events()} == {"telegram"} and len(led.events()) == 3
    text = acts.status_text()
    assert "b_nodw" in text and all(len(ln) <= 48 for ln in text.splitlines())
    assert "#1" in acts.status_text(narrow=False) and (demo / "data" / "board" / "last_board.json").exists()
    assert acts.events_text(2).count("\n") == 1
    cmp = acts.compare_text(["a@demo", "b_nodw@demo"]).splitlines()
    assert cmp[0].split() == ["metric", "a", "b_nodw"] and cmp[1].split() == ["area.cell", "1031.5", "999.0"]
    assert cmp[2].split() == ["power_w[k_small]", "0.25", "-"]
    assert acts.metric_text("area.cell", None).count("\n") == 3 and acts.metric_text("area.cell", "zzz") == "no metrics"
    assert acts.hosts_text().splitlines()[1].startswith("local ")
    lic = acts.lic_text().splitlines()
    assert lic[0].split()[:3] == ["licence", "feature", "pool"] and lic[1].split()[:7] == ["demo", "demo", "10", "2", "8", "0", "2"]
    capsys.readouterr()


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
    with Ledger(demo / "data" / "edr.db") as led:
        assert led.batches()[0]["retired"] and led.run(b)["phase"] == "ABANDONED:gone"
        assert led.run(q)["state"] == "retired" and led.run(a)["phase"] == "done"
        kinds = [(e["run_id"], e["kind"]) for e in led.events()]
    assert kinds == [(a, "prune"), (a, "retire"), (a, "retire"), (b, "retire"), (q, "retire")]
    assert edr(capsys, "status", "--batch", "demo")[1] == "no runs\n" and "c" in edr(capsys, "status")[1]
    assert edr(capsys, "retire", "--batch", "empty", "--why", "x")[0] == 2


# metrics, export, hosts, lic


def test_metrics_and_export(demo: Path, capsys, tmp_path: Path) -> None:
    a = seed(demo, "a", "done")
    add_metric(demo, a, "area_cell_um2", 1000.0)
    add_metric(demo, a, "wns_ns", -0.5, step=3)
    (demo / "data" / "results" / a / "reports" / "3").mkdir(parents=True)
    (demo / "data" / "results" / a / "reports" / "3" / "area.rpt").write_text("i_top 1000.0\n")
    code, out, _ = edr(capsys, "metrics", "--design", "abc1234", "--csv")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("run_id,label,config,design,stage,step,task,metric") and len(lines) == 3
    assert lines[1].startswith(f"{a},a,demo,abc1234,synth,3,,area_cell_um2,area.cell,1000.0,u,")
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
    with Ledger(demo / "data" / "edr.db") as led:
        assert [e["kind"] for e in led.events()] == ["export"]


def test_hosts_and_lic_probe_local(demo: Path, capsys) -> None:
    code, out, _ = edr(capsys, "hosts")
    assert code == 0 and out.splitlines()[0].split() == ["host", "cores", "ram_gb", "free_gb", "mount", "tools", "runs"]
    assert out.splitlines()[1].split()[0] == "local" and str(demo.parent / "scratch") in out
    code, out, _ = edr(capsys, "--json", "hosts", "--narrow")
    assert code == 0 and json.loads(out)["data"][0]["host"] == "local"
    code, out, _ = edr(capsys, "lic")
    assert code == 0 and out.splitlines()[1].split() == ["demo", "demo", "10", "2", "8", "0", "2", "2"]
    assert out.splitlines()[0].split()[-1] == "note"
    site = demo / "site.toml"
    site.write_text(site.read_text().replace("bash {root}/flow/lmstat.sh", "false"))
    code, out, _ = edr(capsys, "lic")
    assert code == 3 and "unknown" in out


# run (reuse), watch, bad input


def test_run_reuse_dry_and_collect(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    assert edr(capsys, "run", "a@demo")[0] == 1
    assert edr(capsys, "run", "a@demo", "--stage", "nope")[0] == 1
    code, _, err = edr(capsys, "run", "a@demo", "--stage", "export", "--on", "local", "--from", "cts", "--dry-run")
    assert code == 1 and "no resume" in err  # export has no resume command
    code, out, _ = edr(capsys, "--json", "run", "a@demo", "--stage", "pnr", "--on", "local", "--from", "cts", "--dry-run")
    data = json.loads(out)["data"]
    assert code == 0 and data["batch"].startswith("run_") and data["host"] == "local"
    assert data["run_id"] == f"{data['batch'][4:]}_a.pnr_demo_gabc1234"
    spec = data["spec"]
    assert spec["start_at"] == {"stage": "pnr", "checkpoint": "cts"} and [s["name"] for s in spec["stages"]] == ["pnr"]
    assert spec["root"] == spec["stages"][0]["cwd"] and f"pnr {data['run_id']} demo" in spec["stages"][0]["cmd"]
    assert data["run_id"] != a and not bdir(demo, data["batch"]).exists()
    code, _, err = edr(capsys, "run", "a@demo", "--stage", "export", "--on", "mars", "--dry-run")
    assert code == 1
    code, out, _ = edr(capsys, "run", "a@demo", "--collect", "netlist")
    assert code == 0 and "1 files" in out and (demo / "data" / "results" / a / "out" / "11").is_dir()
    code, out, _ = edr(capsys, "run", "a@demo", "--collect", "nope")
    assert code == 3
    with Ledger(demo / "data" / "edr.db") as led:
        assert [e["kind"] for e in led.events()] == ["collect", "collect"]


def test_watch_check_dry_once_and_serve(demo: Path, capsys) -> None:
    b = seed(demo, "b_nodw", "stage:synth", pid=dead_pid())
    state = bdir(demo)
    assert edr(capsys, "watch", "--check")[0] == 1
    before = sorted(p.name for p in state.iterdir())
    code, out, _ = edr(capsys, "watch", "--once", "--dry-run")
    assert code == 0 and f"{b}: running" in out and sorted(p.name for p in state.iterdir()) == before
    assert not (demo / "data" / "board").exists()
    port = 40000 + os.getpid() % 20000
    code, out, _ = edr(capsys, "watch", "--once", "--serve", str(port))
    assert code == 0 and (state.parent / "watch.json").exists() and (demo / "data" / "board" / "status.html").exists()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status.html", timeout=5) as r:
        assert r.status == 200 and b"b_nodw" in r.read()
    assert edr(capsys, "watch", "--check")[0] == 0


def test_bad_input_exits_1(capsys) -> None:
    assert cli.main([]) == 1 and cli.main(["nope"]) == 1 and cli.main(["stop", "x"]) == 1
    assert cli.main(["metrics"]) == 1 and cli.main(["--help"]) == 0
    capsys.readouterr()


def test_import_records_a_foreign_tree(demo: Path, capsys, tmp_path: Path) -> None:
    root = tmp_path / "scratch" / "lkesting" / "edr" / "old" / "20260904_0411_ref_x_gabc1234"
    root.mkdir(parents=True)
    argv = ["import", "--run-id", root.name, "--label", "ref", "--config", "demo", "--src", "abc1234",
            "--host", "local", "--root", str(root), "--why", "reference"]
    code, out, _ = edr(capsys, *argv, "--dry-run")
    assert code == 0 and "(dry)" in out and not (demo / "data" / "edr.db").exists()
    assert edr(capsys, *argv)[0] == 0
    with Ledger(demo / "data" / "edr.db") as led:
        row = led.run(root.name)
        assert row["phase"] == "done" and row["state"] == "imported" and row["root"] == str(root)
        assert [b["batch"] for b in led.batches()] == ["imported"]
        assert led.events()[-1]["kind"] == "import"
    assert edr(capsys, "import", "--run-id", "bad", "--label", "r", "--config", "demo", "--src", "a",
               "--host", "local", "--root", str(root))[0] == 1
    assert edr(capsys, "import", "--run-id", root.name, "--label", "r", "--config", "demo", "--src", "a",
               "--host", "local", "--root", str(root / "missing"))[0] == 1


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
    hb.update(phase="INCOMPLETE:1f0s", exit=8, updated=now, counts={"done": 0, "failed": 1})
    hb_path.write_text(json.dumps(hb))
    code, out, _ = edr(capsys, "status")
    assert code == 0 and "INCOMPLETE:1f0s" in out
    code, out, _ = edr(capsys, "status", "--triage")
    assert code == 0 and "incomplete" in out and "b_nodw@demo" in out
    code, out, _ = edr(capsys, "status", "b_nodw@demo")
    assert code == 0 and "INCOMPLETE:1f0s" in out
    with Ledger(demo / "data" / "edr.db") as led:
        assert led.run(b)["phase"] == "INCOMPLETE:1f0s" and led.run(b)["exit"] == 8


def test_run_on_an_imported_tree_needs_no_jobs_file(demo: Path, capsys, tmp_path: Path) -> None:
    root = tmp_path / "scratch" / "lkesting" / "edr" / "old" / "20260904_0411_ref_demo_gabc1234"
    root.mkdir(parents=True)
    assert edr(capsys, "import", "--run-id", root.name, "--label", "ref", "--config", "demo", "--src", "abc1234",
               "--host", "local", "--root", str(root))[0] == 0
    code, out, err = edr(capsys, "run", "ref@imported", "--stage", "power", "--tasks", "k_small", "--on", "local",
                         "--dry-run")
    assert code == 0, (out, err)
    assert "(dry)" in out and "ref.power" in out


def test_retire_refuses_a_root_that_another_run_uses(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    with Ledger(demo / "data" / "edr.db") as led:
        row = led.run(a)
        led.upsert_run({"run_id": f"{DATE}_b_nodw_demo_gabc1234", "batch": "demo", "label": "b_nodw", "config": "demo",
                        "host": row["host"], "root": row["root"], "src": "abc1234", "phase": "done", "state": "done"})
    code, out, err = edr(capsys, "retire", "a@demo", "--why", "t", "--uncollected")
    assert code == 1 and "shared with another run" in err
    assert Path(row["root"]).exists()
    code, out, err = edr(capsys, "retire", "a@demo", "--why", "t", "--prune", "netlist", "--dry-run")
    assert code == 0, err  # a prune of a shared root is fine while no sharer is live
    code, out, err = edr(capsys, "retire", "--batch", "demo", "--why", "t", "--uncollected")
    assert code == 0, err
    assert not Path(row["root"]).exists()

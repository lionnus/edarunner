"""Two copies of the demo under one supervisor on the host local: the census, the placement and the seats of both."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from helpers_cli import edr
from helpers_watch import Rec

from edarunner import board, config, home, serve
from edarunner.db import Database
from helpers_driver import DEMO


def project(tmp_path: Path, name: str, **limits: int) -> Path:
    """A copy of the demo as project `name`, its jobs placed by `auto`, its scratch under tmp_path."""
    root = tmp_path / "edr" / name
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    text = (root / "edr.toml").read_text().replace('project = "demo"', f'project = "{name}"')
    text = text.replace("gate_max_s = 30", "gate_max_s = 300")
    for key, value in limits.items():
        text = text.replace(f"{key} = 4", f"{key} = {value}")
    (root / "edr.toml").write_text(text)
    (root / "jobs" / "demo.toml").write_text((root / "jobs" / "demo.toml").read_text().replace('host = "local"', 'host = "auto"'))
    site = root / "site.toml"
    site.write_text(site.read_text().replace('"/tmp/edr-demo"', f'"{tmp_path / "scratch"}"'))
    subprocess.run(["bash", "setup.sh"], cwd=root, check=True, capture_output=True)
    return root


def test_two_projects_share_the_hosts_and_the_seats_under_one_supervisor(tmp_path: Path, capsys, monkeypatch) -> None:
    (tmp_path / "scratch").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_SEATS_USED", "9")  # the demo probe reports one free seat
    alpha, beta = project(tmp_path, "alpha"), project(tmp_path, "beta", max_per_host=3)
    for root, want in ((alpha, "2 started, 0 queued"), (beta, "1 started, 1 queued")):
        monkeypatch.chdir(root)
        assert edr(capsys, "checkout", "HEAD")[0] == 0
        code, out, _ = edr(capsys, "launch", "demo")
        assert code == 0 and want in out, out  # beta counts the two runs of alpha on local
    monkeypatch.chdir(tmp_path)
    rec = Rec()
    sup = serve.Supervisor([rec])
    leases, seen_gate, seen_both, end = home.root() / "leases" / "demo", False, False, time.time() + 240
    while time.time() < end:
        sup.cycle(time.time(), once=True)
        assert len(list(leases.glob("*"))) <= 1  # one seat: the drivers of both projects take turns
        runs = json.loads(edr(capsys, "--json", "status", "--all")[1])["data"]["runs"]
        seen_gate |= any(r.get("phase") == "gate:synth" for r in runs)
        census = config.load_json(home.root() / "census.json")
        seen_both |= {r["project"] for r in census.get("runs") or []} == {"alpha", "beta"}
        if len(runs) == 4 and not any(board.is_live(r) for r in runs):
            break
        time.sleep(1)
    else:
        raise AssertionError(f"still live: {[(r['project'], r['label'], r['phase']) for r in runs]}")
    sup.cycle(time.time(), once=True)
    assert sorted((r["project"], r["label"], r["phase"]) for r in runs) == [
        ("alpha", "a", "done"), ("alpha", "b_nodw", "done"), ("beta", "a", "done"), ("beta", "b_nodw", "done")]
    assert seen_gate and seen_both and list(leases.glob("*")) == []
    for root in (alpha, beta):
        with Database(root / "data" / "edr.db") as db:
            ids = {r["run_id"] for r in db.runs()}
            assert {m["run_id"] for m in db.metrics()} <= ids and len(ids) == 2
        assert {p.name for p in (root / "data" / "results").iterdir()} == ids
    # A stray sleep of this machine belongs to no project; no process of a run of alpha or beta is an orphan.
    assert not [a.about for a in rec.alerts.values() if a.kind == "orphan" and "no edarunner run owns it" not in a.about]
    assert "<b>alpha</b> <i>nothing live" in rec.boards[-1] and "<b>beta</b> <i>nothing live" in rec.boards[-1]
    state = json.loads((home.root() / "serve.json").read_text())
    assert set(state["projects"]) == {"alpha", "beta"} and state["cycle"] >= 2

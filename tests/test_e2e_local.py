"""The demo end to end through cli.main on the host local; every write lands under tmp_path."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from edarunner import board, cli, config
from edarunner.ledger import Ledger
from test_cli import demo, edr  # noqa: F401  (the fixture and the runner of test_cli)


def setup_repo(root: Path) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=root, check=True, capture_output=True)


def runs(capsys) -> list[dict]:
    code, out, _ = edr(capsys, "--json", "status")
    assert code == 0
    return json.loads(out)["data"]["runs"]


def wait_terminal(capsys, n: int, timeout: float = 90) -> list[dict]:
    """watch --once until `n` runs exist and none is live; the last cycle sees the terminal heartbeats."""
    end = time.time() + timeout
    while time.time() < end:
        rows = runs(capsys)
        terminal = len(rows) >= n and not any(board.is_live(r) for r in rows)
        assert cli.main(["watch", "--once"]) == 0
        capsys.readouterr()
        if terminal:
            return runs(capsys)
        time.sleep(1)
    raise AssertionError(f"runs still live after {timeout} s: {[(r['label'], r['phase']) for r in runs(capsys)]}")


def listing(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()} if root.exists() else {}


def test_demo_end_to_end(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    setup_repo(demo)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    assert edr(capsys, "init", "--site", str(demo))[0] == 0
    assert config.load_project(fresh).site.path == demo / "site.toml"
    monkeypatch.chdir(demo)
    code, out, _ = edr(capsys, "check")
    assert code == 0 and out.startswith("ok:")
    code, out, _ = edr(capsys, "--json", "stage", "HEAD")
    src = json.loads(out)["data"]["src"]
    assert code == 0 and re.fullmatch(r"[0-9a-f]{7,}", src) and (demo / "wt" / src / "flow" / "flow.sh").is_file()
    state = tmp_path / ".edr" / "demo"
    assert edr(capsys, "plan", "demo", "--dry-run")[0] == 0 and not state.exists()
    code, out, _ = edr(capsys, "plan", "demo")
    assert code == 0 and f"_a_demo_g{src}: local " in out and f"_b_nodw_demo_DW0_g{src}: local " in out
    assert not state.exists()

    code, out, _ = edr(capsys, "launch", "demo")
    assert code == 0 and "2 started" in out
    date = (state / "demo" / "RUN_DATE").read_text().strip()
    ids = {"a": f"{date}_a_demo_g{src}", "b_nodw": f"{date}_b_nodw_demo_DW0_g{src}"}
    (driver,) = (state / "bin").iterdir()
    spec = json.loads((state / "demo" / f"{ids['a']}.spec.json").read_text())
    assert driver.name.startswith("edr_driver-") and spec["driver"] == str(driver)
    assert edr(capsys, "launch", "demo")[0] == 2  # already launched
    code, out, _ = edr(capsys, "keep", "a@demo", "--hours", "1", "--ack")
    assert code == 0 and json.loads((state / "demo" / f"{ids['a']}.keep.json").read_text()) == {"hours": 1, "ack": True}

    rows = wait_terminal(capsys, 2)
    assert {r["run_id"]: r["phase"] for r in rows} == {ids["a"]: "done", ids["b_nodw"]: "done"}
    roots = {r["label"]: Path(r["root"]) for r in rows}
    assert roots["a"] == tmp_path / "scratch" / config.placeholders(config.load_project(demo))["user"] / "edr" / "demo" / ids["a"]
    hb = json.loads((state / "demo" / f"{ids['a']}.json").read_text())
    assert hb["exit"] == 0 and hb["keep_hours"] == 1 and hb["counts"]["done"] == 2 and hb["counts"]["failed"] == 0
    results = demo / "data" / "results"
    assert (results / ids["a"] / "reports" / "6" / "area.rpt").is_file() and (results / ids["a"] / "log" / "power.k_big.log").is_file()
    assert (results / ids["a"] / "simulation" / "tests" / "demo" / "GEMM_M64_N64" / "power" / "reports" / "power.csv").is_file()
    assert (results / ids["b_nodw"] / "reports" / "5" / "qor.rpt").is_file()

    code, out, _ = edr(capsys, "status")
    assert code == 0 and "done" in out and "b_nodw" in out
    code, out, _ = edr(capsys, "status", "a@demo")
    assert code == 0 and out.startswith(ids["a"]) and "done" in out and "energy" in out and "area.cell" in out
    code, out, _ = edr(capsys, "status", "--narrow")
    assert code == 0 and "nothing live" in out
    code, out, _ = edr(capsys, "metrics", "--design", src, "--csv")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("run_id,label,config,design,stage,step,task,metric")
    assert any(",power,,k_small,energy_nj,energy,850.0,nJ," in ln for ln in lines)
    assert any(f"{ids['b_nodw']},b_nodw,demo,{src},pnr,5,,area_cell_um2,area.cell,1052.5,um2,reports/5/area.rpt" == ln for ln in lines)
    with Ledger(demo / "data" / "edr.db") as led:
        params = {tuple(r) for r in led.db.execute("SELECT run_id, key, value FROM params")}
    assert (ids["b_nodw"], "DW", "0") in params and (ids["a"], "src", src) in params

    exp = tmp_path / "exp"
    code, out, _ = edr(capsys, "export", "--design", src, "--out", str(exp))
    manifest = json.loads((exp / "manifest.json").read_text())
    assert code == 0 and {r["label"] for r in manifest["runs"]} == {"a", "b_nodw"} and manifest["incomplete"] == []
    assert (exp / "a" / "reports" / "6" / "area.rpt").is_file() and (exp / "runs.csv").is_file() and manifest["source"] == src
    code, out, _ = edr(capsys, "stop", "a@demo", "--after-task", "--why", "test")
    assert code == 2 and "already done" in out

    code, out, _ = edr(capsys, "--json", "run", "a@demo", "--stage", "export", "--on", "local")
    new = json.loads(out)["data"]
    assert code == 0 and new["batch"] == "demo" and new["root"] == str(roots["a"])
    assert re.fullmatch(rf"\d{{8}}_\d{{4}}_a\.export_demo_g{src}", new["run_id"]) and new["run_id"] != ids["a"]
    rows = wait_terminal(capsys, 3)
    assert {r["run_id"]: (r["label"], r["phase"]) for r in rows}[new["run_id"]] == ("a.export", "done")
    assert [p for p in (state / "bin").iterdir()] == [driver]  # one copy per driver version
    assert (roots["a"] / "out" / "11" / "netlist.v").is_file() and (results / new["run_id"] / "log" / "export.log").is_file()

    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--why", "test", "--dry-run")
    assert code == 0 and all(p.is_dir() for p in roots.values()) and not (state / "demo" / "RETIRED").exists()
    assert out.count("rm -rf ") == 3 and "(dry)" in out  # the export run names the tree of a again
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--why", "test")
    assert code == 0 and not any(p.exists() for p in roots.values()) and (state / "demo" / "RETIRED").is_file()
    assert (state / "demo" / f"{ids['a']}.json").is_file()  # the state outlives the tree
    assert not (demo / "wt" / src).exists() and (demo / "repo" / "flow").is_dir()  # no batch has the source now
    assert runs(capsys) == []  # the export run went with its batch
    code, out, _ = edr(capsys, "--json", "events", "-n", "100")
    kinds = [(e["actor"], e["kind"]) for e in json.loads(out)["data"]]
    assert kinds.count(("user", "launch")) == 2 and ("user", "keep") in kinds and ("user", "run") in kinds
    assert kinds.count(("user", "retire")) == 4 and ("user", "export") in kinds and ("watch", "done") in kinds


def test_dry_run_flow_writes_nothing(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    setup_repo(demo)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    assert edr(capsys, "init", "--site", str(demo), "--dry-run")[0] == 0 and list(fresh.iterdir()) == []
    monkeypatch.chdir(demo)
    assert edr(capsys, "stage", "HEAD", "--dry-run")[0] == 0 and not (demo / "wt").exists()
    src = json.loads(edr(capsys, "--json", "stage", "HEAD")[1])["data"]["src"]  # the fixture worktree
    state, scratch = tmp_path / ".edr", tmp_path / "scratch"
    wt = listing(demo / "wt")
    assert edr(capsys, "stage", "HEAD", "--dry-run")[0] == 0
    assert edr(capsys, "plan", "demo", "--dry-run")[0] == 0
    code, out, _ = edr(capsys, "launch", "demo", "--dry-run")
    assert code == 0 and "(dry)" in out and "rsync -a --delete" in out
    assert edr(capsys, "watch", "--once", "--dry-run")[0] == 0
    assert not (demo / "data").exists()  # a dry run opens no ledger file
    assert edr(capsys, "check")[0] == 0
    assert edr(capsys, "status")[1] == "no runs\n"
    assert edr(capsys, "metrics", "--design", src, "--csv")[0] == 2
    assert edr(capsys, "export", "--design", src, "--out", str(tmp_path / "exp"), "--dry-run")[0] == 1
    assert edr(capsys, "retire", "--batch", "demo", "--why", "x", "--dry-run")[0] == 2
    assert not state.exists() and list(scratch.iterdir()) == [] and listing(demo / "wt") == wt
    assert not (tmp_path / "exp").exists() and not (demo / "data" / "board").exists()
    assert not (demo / "data" / "results").exists()
    with Ledger(demo / "data" / "edr.db") as led:
        assert led.runs() == [] and led.events() == [] and led.batches() == []

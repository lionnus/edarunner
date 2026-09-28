"""The demo end to end through cli.main on the host local; every write lands under tmp_path."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from helpers_cli import edr

from edarunner import board, cli, config
from edarunner.db import Database


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
    code, out, _ = edr(capsys, "--json", "checkout", "HEAD")
    source = json.loads(out)["data"]["source"]
    assert code == 0 and re.fullmatch(r"[0-9a-f]{7,}", source) and (demo / "wt" / source / "flow" / "flow.sh").is_file()
    state = tmp_path / ".edr" / "demo"
    assert edr(capsys, "plan", "demo", "--dry-run")[0] == 0 and not state.exists()
    code, out, _ = edr(capsys, "plan", "demo")
    assert code == 0 and f"_a_demo_g{source}: local " in out and f"_b_nodw_demo_DW0_g{source}: local " in out
    code, out, _ = edr(capsys, "plan", "demo", "--show-spec")
    assert code == 0 and f"  env:\n    " in out and f"    EDR_SOURCE={source}\n" in out and "    collect: reports/\n" in out
    assert "  stage synth, in " in out and "    cmd: bash " in out
    code, out, _ = edr(capsys, "plan", "demo", "--json")
    assert code == 0 and len(json.loads(out)["data"]) == 2
    assert not state.exists() and not (demo / "data" / "edr.db").exists()

    code, out, _ = edr(capsys, "launch", "demo")
    assert code == 0 and "2 started" in out
    date = (state / "demo" / "RUN_DATE").read_text().strip()
    ids = {"a": f"{date}_a_demo_g{source}", "b_nodw": f"{date}_b_nodw_demo_DW0_g{source}"}
    (driver,) = (state / "bin").iterdir()
    spec = json.loads((state / "demo" / f"{ids['a']}.spec.json").read_text())
    assert driver.name.startswith("edr_driver-") and spec["driver"] == str(driver)
    assert edr(capsys, "launch", "demo")[0] == 2  # already launched
    code, out, _ = edr(capsys, "keep", "a@demo", "--hours", "1")
    assert code == 0 and json.loads((state / "demo" / f"{ids['a']}.keep.json").read_text()) == {"hours": 1}

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
    assert code == 0 and out.startswith(ids["a"]) and "done" in out and "energy_nj" in out and "area_cell_um2" in out and "design__instance__area" not in out
    code, out, _ = edr(capsys, "status", "--narrow")
    assert code == 0 and "nothing live" in out
    code, out, _ = edr(capsys, "metrics", "--source", source, "--csv")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("run_id,label,config,source,stage,step,task,metric")
    assert any(",power,,k_small,energy_nj,,850.0,nJ," in ln for ln in lines)
    assert any(f"{ids['b_nodw']},b_nodw,demo,{source},pnr,5,,area_cell_um2,design__instance__area,1052.5,um2,reports/5/area.rpt:1,1"
               == ln for ln in lines)
    # The area of record is the deepest pnr step from route on, step 5, in both runs.
    code, out, _ = edr(capsys, "compare", "a@demo", "b_nodw@demo", "--metric", "area_cell_um2")
    row = next(ln.split() for ln in out.splitlines() if ln.startswith("area_cell_um2"))
    assert code == 0 and row == ["area_cell_um2", "1052.5", "(pnr", "5)", "1052.5", "(pnr", "5)", "0", "+0.0%"]
    # The regexes of the demo read the qor.rpt that flow.sh writes, and the pass rule gives the verdict.
    code, out, _ = edr(capsys, "metrics", "--run", ids["b_nodw"], "--over", "steps")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0] == ["stage", "step", "name", "area_cell_um2", "setup_violations", "wns_ns", "verdict"]
    assert lines[2] == ["synth", "0", "setup", "1000", "0", "0", "pass"]
    assert lines[-1] == ["pnr", "5", "route", "1052.5", "5", "FAIL", "-0.05", "FAIL"]
    with Database(demo / "data" / "edr.db") as db:
        params = {tuple(r) for r in db.conn.execute("SELECT run_id, key, value FROM parameters")}
    assert (ids["b_nodw"], "DW", "0") in params and (ids["a"], "source", source) in params
    code, out, _ = edr(capsys, "--json", "compare", "a@demo", "b_nodw@demo")
    assert {p["key"]: list(p["value"].values()) for p in json.loads(out)["data"]["parameters"]} == {
        "DW": [None, "0"], "build_tag": ["demo", "demo_DW0"], "vars.netlist_stage": ["11", None]}

    exp = tmp_path / "exp"
    code, out, _ = edr(capsys, "export", "--source", source, "--out", str(exp))
    manifest = json.loads((exp / "manifest.json").read_text())
    assert code == 0 and {r["label"] for r in manifest["runs"]} == {"a", "b_nodw"} and manifest["incomplete"] == []
    assert (exp / "a" / "reports" / "6" / "area.rpt").is_file() and (exp / "runs.csv").is_file() and manifest["sources"] == [source]
    code, out, _ = edr(capsys, "stop", "a@demo", "--after-task", "--why", "test")
    assert code == 2 and "already done" in out

    code, out, _ = edr(capsys, "--json", "continue", "a@demo", "--stage", "export", "--on", "local")
    new = json.loads(out)["data"]
    assert code == 0 and new["batch"] == "demo" and new["root"] == str(roots["a"])
    assert re.fullmatch(rf"\d{{8}}_\d{{4}}_a\.export_demo_g{source}", new["run_id"]) and new["run_id"] != ids["a"]
    rows = wait_terminal(capsys, 3)
    assert {r["run_id"]: (r["label"], r["phase"]) for r in rows}[new["run_id"]] == ("a.export", "done")
    assert [p for p in (state / "bin").iterdir()] == [driver]  # one copy per driver version
    assert (roots["a"] / "out" / "11" / "netlist.v").is_file() and (results / new["run_id"] / "log" / "export.log").is_file()

    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--why", "test", "--dry-run")
    assert code == 0 and all(p.is_dir() for p in roots.values()) and not (state / "demo" / "RETIRED").exists()
    # Three run trees, the export run naming the tree of a again, and the checked-out clone.
    assert out.count("rm -rf ") == 4 and "(dry)" in out
    code, out, _ = edr(capsys, "retire", "--batch", "demo", "--why", "test")
    assert code == 0 and not any(p.exists() for p in roots.values()) and (state / "demo" / "RETIRED").is_file()
    assert (state / "demo" / f"{ids['a']}.json").is_file()  # the state outlives the tree
    assert not (demo / "wt" / source).exists() and (demo / "repo" / "flow").is_dir()  # no batch has the source now
    assert runs(capsys) == []  # the export run went with its batch
    code, out, _ = edr(capsys, "--json", "events", "-n", "100")
    kinds = [(e["actor"], e["kind"]) for e in json.loads(out)["data"]]
    assert kinds.count(("user", "launch")) == 2 and ("user", "keep") in kinds and ("user", "continue") in kinds
    assert kinds.count(("user", "retire")) == 4 and ("user", "export") in kinds and ("watch", "done") in kinds


def test_dry_run_flow_writes_nothing(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    setup_repo(demo)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    assert edr(capsys, "init", "--site", str(demo), "--dry-run")[0] == 0 and list(fresh.iterdir()) == []
    monkeypatch.chdir(demo)
    assert edr(capsys, "checkout", "HEAD", "--dry-run")[0] == 0 and not (demo / "wt").exists()
    source = json.loads(edr(capsys, "--json", "checkout", "HEAD")[1])["data"]["source"]  # the fixture clone
    state, scratch = tmp_path / ".edr", tmp_path / "scratch"
    wt = listing(demo / "wt")
    assert edr(capsys, "checkout", "HEAD", "--dry-run")[0] == 0
    assert edr(capsys, "plan", "demo", "--dry-run")[0] == 0
    code, out, _ = edr(capsys, "launch", "demo", "--dry-run")
    assert code == 0 and "(dry)" in out and "rsync -a --delete" in out
    assert edr(capsys, "watch", "--once", "--dry-run")[0] == 0
    assert not (demo / "data").exists()  # a dry run opens no database file
    assert edr(capsys, "check")[0] == 0
    assert edr(capsys, "status")[1] == "no runs\n"
    assert edr(capsys, "metrics", "--source", source, "--csv")[0] == 2
    assert edr(capsys, "export", "--source", source, "--out", str(tmp_path / "exp"), "--dry-run")[0] == 1
    assert edr(capsys, "retire", "--batch", "demo", "--why", "x", "--dry-run")[0] == 2
    assert not state.exists() and list(scratch.iterdir()) == [] and listing(demo / "wt") == wt
    assert not (tmp_path / "exp").exists() and not (demo / "data" / "board").exists()
    assert not (demo / "data" / "results").exists()
    with Database(demo / "data" / "edr.db") as db:
        assert db.runs() == [] and db.events() == [] and db.batches() == []


def test_plan_and_launch_check_out_a_missing_source(demo: Path, capsys, tmp_path: Path) -> None:
    setup_repo(demo)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=demo / "repo", check=True,
                          capture_output=True, text=True).stdout.strip()
    code, out, _ = edr(capsys, "plan", "demo", "--dry-run")
    assert code == 0 and f"git clone --local --no-checkout {demo / 'repo'} {demo / 'wt' / head}" in out
    assert f"checkout {head} {demo / 'wt' / head} (dry)" in out and not (demo / "wt").exists()
    code, out, err = edr(capsys, "launch", "demo", "--dry-run")
    assert code == 0, out + err
    assert f"checkout {head} {demo / 'wt' / head} (dry)" in out and not (demo / "wt").exists()
    code, out, _ = edr(capsys, "plan", "demo")
    assert code == 0 and f"checkout {head} {demo / 'wt' / head}\n" in out and f"_a_demo_g{head}: local " in out
    assert (demo / "wt" / head / "flow" / "flow.sh").is_file()
    code, out, _ = edr(capsys, "plan", "demo")
    assert code == 0 and "checkout " not in out


def test_plan_refuses_a_dirty_source_that_is_not_checked_out(demo: Path, capsys) -> None:
    setup_repo(demo)
    (demo / "jobs" / "demo.toml").write_text(
        (demo / "jobs" / "demo.toml").read_text().replace('source = "HEAD"', 'source = "abc1234-dirty-0123abcd"'))
    for cmd in (["plan", "demo"], ["launch", "demo", "--dry-run", "--allow-dirty"]):
        code, out, err = edr(capsys, *cmd)
        assert code == 1 and "not checked out" in err and "edr checkout --dirty" in err and "checkout " not in out
    assert not (demo / "wt").exists()

"""Tests of metrics.extract on the demo layout."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest
from helpers_results import QOR, demo_qor

from edarunner.metrics import extract
from edarunner.model import Limits, Metric, Placement, Project, Safety, Site, Source, Stage, Sync, Task
from helpers_driver import DEMO

RUN_ID = "20260926_1200_a_demo_gabc1234"
RUN = {"run_id": RUN_ID, "batch": "demo", "label": "a", "config": "demo", "host": "local", "tasks": {}}
COLUMNS = {"run_id", "stage", "step", "task", "name", "canonical", "value", "unit", "source_file", "extracted_at"}


def demo_project(root: Path) -> Project:
    """Build a Project from the demo edr.toml with the parts extract reads; the demo hooks go to `root`."""
    shutil.copytree(DEMO / "hooks", root / "hooks", dirs_exist_ok=True)
    cfg = tomllib.loads((DEMO / "edr.toml").read_text())
    stages = {
        n: Stage(name=n, foreach=s.get("foreach", ""), task_dir=s.get("task_dir", ""), steps=s.get("steps", []))
        for n, s in cfg["stages"].items()
    }
    metrics = {}
    for n, m in cfg["metrics"].items():
        stage = m.get("stage", [])
        metrics[n] = Metric(
            name=n,
            stage=[stage] if isinstance(stage, str) else stage,
            step=None if "step" not in m else str(m["step"]),
            file=m.get("file", ""),
            regex=m.get("regex", ""),
            reduce=m.get("reduce", "first"),
            pass_=m.get("pass", ""),
            csv=m.get("csv"),
            json=m.get("json", ""),
            python=m.get("python", ""),
            unit=m.get("unit", ""),
            canonical=m.get("canonical", ""),
        )
    site = Site(path=DEMO / "site.toml", scratch=[], env={}, ssh_options=[], ssh_timeout_s=1, tool_procs="", hosts={})
    source = Source(repo=root, worktrees=root, ref="HEAD", nested=[], run_id="", build_tag="")
    return Project(
        root=root, project="demo", site=site, state_dir=root, data=root, run_prefix="", source=source,
        sync=Sync(exclude=[]), safety=Safety(marker="/edr/"), limits=Limits(), placement=Placement(),
        stages=stages, metrics=metrics,
    )


def demo_tasks() -> dict[str, Task]:
    cfg = tomllib.loads((DEMO / "tasks.toml").read_text())
    tasks = {i: Task(id=i, fields={k: v for k, v in t.items() if isinstance(v, str)}) for i, t in cfg["tasks"].items()}
    return {i: tasks[i] for i in ("k_small", "k_big")}


def results_tree(results: Path) -> Path:
    """Write what flow.sh and kernel.sh write: steps 0..3 of synth, 4..5 of pnr, two tasks."""
    run = results / RUN_ID
    for n in range(6):
        d = run / "reports" / str(n)
        d.mkdir(parents=True)
        (d / "area.rpt").write_text(f"i_top {1000 + n * 10.5:.1f}\n")
        (d / "qor.rpt").write_text(demo_qor(n))
    for test in ("GEMM_M64_N64", "SOFTMAX_R197"):
        p = run / "simulation" / "tests" / "demo" / test / "power"
        (p / "reports").mkdir(parents=True)
        (p / "reports" / "power.csv").write_text("phase,total_w\nPHASE_A,0.100\nPHASE_B,0.150\nWHOLE,0.250\n")
        (p / "phases.json").write_text(json.dumps({"window_ns": 3400, "kernel": "x"}))
    return run


def by_name(rows: list[dict], name: str, stage: str | None = None) -> list[dict]:
    return [r for r in rows if r["name"] == name and (stage is None or r["stage"] == stage)]


def test_demo_metrics(tmp_path):
    results_tree(tmp_path / "results")
    rows = extract(demo_project(tmp_path), RUN, tmp_path / "results", demo_tasks())
    assert rows and all(set(r) == COLUMNS and r["run_id"] == RUN_ID for r in rows)
    area = by_name(rows, "area_cell_um2", "synth")
    assert [(r["step"], r["value"]) for r in area] == [(0, 1000.0), (1, 1010.5), (2, 1021.0), (3, 1031.5)]
    assert area[0]["task"] == "" and area[0]["unit"] == "um2" and area[0]["canonical"] == "design__instance__area"
    assert area[0]["source_file"] == "reports/0/area.rpt:1"
    assert [(r["value"], r["source_file"]) for r in by_name(rows, "wns_ns", "pnr")] == [
        (-0.04, "reports/4/qor.rpt:18"), (-0.05, "reports/5/qor.rpt:18")]
    assert [(r["value"], r["source_file"]) for r in by_name(rows, "setup_violations", "pnr")] == [
        (4.0, "reports/4/qor.rpt:12"), (5.0, "reports/5/qor.rpt:12")]
    assert [r["step"] for r in by_name(rows, "area_cell_um2", "pnr")] == [4, 5]
    assert {r["task"]: r["value"] for r in by_name(rows, "power_w")} == {"k_small": 0.25, "k_big": 0.25}
    power = by_name(rows, "power_w")[0]
    assert power["stage"] == "power" and power["step"] is None and power["canonical"] == "power__total"
    assert power["source_file"] == "simulation/tests/demo/GEMM_M64_N64/power/reports/power.csv"
    assert {r["task"]: r["value"] for r in by_name(rows, "window_ns")} == {"k_small": 3400.0, "k_big": 3400.0}
    energy = by_name(rows, "energy_nj")
    assert {r["task"]: r["value"] for r in energy} == {"k_small": 850.0, "k_big": 850.0}
    assert energy[0]["stage"] == "power" and energy[0]["step"] is None
    assert energy[0]["unit"] == "nJ" and energy[0]["canonical"] == "energy"
    assert len(rows) == 6 * 3 + 2 + 2 + 2


def test_missing_file_is_skipped_and_bad_file_is_a_none_row(tmp_path):
    run = results_tree(tmp_path / "results")
    (run / "reports" / "2" / "qor.rpt").unlink()
    (run / "reports" / "1" / "area.rpt").write_text("garbage\n")
    (run / "simulation/tests/demo/SOFTMAX_R197/power/phases.json").unlink()
    (run / "simulation/tests/demo/GEMM_M64_N64/power/reports/power.csv").write_text("phase,total_w\nPHASE_A,0.1\n")
    rows = extract(demo_project(tmp_path), RUN, tmp_path / "results", demo_tasks())
    assert [r["step"] for r in by_name(rows, "wns_ns", "synth")] == [0, 1, 3]
    bad = [r for r in by_name(rows, "area_cell_um2", "synth") if r["step"] == 1]
    assert len(bad) == 1 and bad[0]["value"] is None
    assert bad[0]["source_file"].startswith("reports/1/area.rpt: ") and "no match" in bad[0]["source_file"]
    csv_bad = [r for r in by_name(rows, "power_w") if r["task"] == "k_small"]
    assert csv_bad[0]["value"] is None and "WHOLE" in csv_bad[0]["source_file"]
    assert [r["task"] for r in by_name(rows, "window_ns")] == ["k_small"]
    (energy,) = by_name(rows, "energy_nj")
    assert energy["task"] == "k_small" and energy["value"] is None and "no WHOLE row" in energy["source_file"]


def test_python_parser_and_fixed_step(tmp_path):
    results_tree(tmp_path / "results")
    (tmp_path / "hooks").mkdir(exist_ok=True)
    (tmp_path / "hooks" / "p.py").write_text("def read(path):\n    return open(path).read().split()[1]\n")
    project = demo_project(tmp_path)
    project.metrics = {
        "area_py": Metric(name="area_py", stage=["synth"], step="2", file="reports/{step}/area.rpt",
                          python="hooks/p.py:read", unit="um2"),
    }
    rows = extract(project, RUN, tmp_path / "results", {})
    assert [(r["stage"], r["step"], r["value"], r["source_file"]) for r in rows] == [
        ("synth", 2, 1021.0, "reports/2/area.rpt"),
    ]


def test_a_numbered_step_belongs_to_one_stage(tmp_path):
    run = tmp_path / "results" / RUN_ID
    for n in range(9):
        (run / "reports" / str(n)).mkdir(parents=True)
        (run / "reports" / str(n) / "area.rpt").write_text(f"i_top {n}\n")
    project = demo_project(tmp_path)
    project.stages = {
        "synth": Stage(name="synth", steps=["a", "b", "c", "d"]),
        "pnr": Stage(name="pnr", steps=["a", "b", "c", "d", "e", "f"]),
        "export": Stage(name="export", steps=["x", "y", "z"]),
        "nosteps": Stage(name="nosteps"),
    }
    project.metrics = {
        "area": Metric(name="area", stage=list(project.stages), step="*", file="reports/{step}/area.rpt",
                       regex=r"i_top (\S+)"),
        "fixed": Metric(name="fixed", stage=["synth", "pnr"], step="2", file="reports/{step}/area.rpt",
                        regex=r"i_top (\S+)"),
    }
    rows = extract(project, RUN, tmp_path / "results", {})
    steps = lambda stage, name="area": [r["step"] for r in by_name(rows, name, stage)]
    assert steps("synth") == [0, 1, 2, 3] and steps("pnr") == [4, 5]
    assert steps("export") == [6, 7, 8] and steps("nosteps") == []
    assert steps("synth", "fixed") == [2] and steps("pnr", "fixed") == []


def test_a_metric_file_follows_the_placeholder_rules_of_the_stage_strings(tmp_path):
    run = results_tree(tmp_path / "results")
    (run / "${OUT}").mkdir()
    (run / "${OUT}" / "area.rpt").write_text("i_top 7.0\n")
    project = demo_project(tmp_path)
    project.metrics = {
        "shell": Metric(name="shell", stage=["synth"], file="${OUT}/area.rpt", regex=r"i_top (\S+)"),
        "bad": Metric(name="bad", stage=["synth"], file="{nope}/area.rpt", regex=r"i_top (\S+)"),
    }
    rows = extract(project, RUN, tmp_path / "results", {})
    assert [(r["name"], r["value"]) for r in rows] == [("shell", 7.0), ("bad", None)]
    assert rows[1]["source_file"] == "unknown placeholder {nope} in '{nope}/area.rpt'"


def test_a_regex_reads_one_block_of_a_report_and_names_its_line(tmp_path):
    (tmp_path / "results" / RUN_ID).mkdir(parents=True)
    (tmp_path / "results" / RUN_ID / "qor.rpt").write_text(QOR)
    setup = r"^Scenario\s+'func_slow'\n(?:.*\n)*?"
    project = demo_project(tmp_path)
    project.metrics = {name: Metric(name=name, stage=["synth"], file="qor.rpt", regex=regex, **kw) for name, regex, kw in (
        # The hold block of reg2reg has no setup slack, so a lazy regex runs on into the next block.
        ("lazy", r"Timing Path Group\s+'reg2reg'[\s\S]*?Critical Path Slack:\s+(\S+)", {}),
        ("reg2reg", r"^Scenario\s+'func_slow'\nTiming Path Group\s+'reg2reg'\n(?:.*\n)*?Critical Path Slack:\s+(\S+)", {}),
        *((how, setup + r"Critical Path Slack:\s+(\S+)", {"reduce": how}) for how in ("first", "last", "min", "max")),
        ("fails", setup + r"No\. of Violating Paths:\s+(\d+)", {"reduce": "sum"}),
        ("none", r"^Scenario\s+'func_typ'\n(?:.*\n)*?Critical Path Slack:\s+(\S+)", {"reduce": "min"}),
    )}
    rows = {r["name"]: (r["value"], r["source_file"]) for r in extract(project, RUN, tmp_path / "results", {})}
    assert rows == {"lazy": (0.004, "qor.rpt:11"), "reg2reg": (-0.031, "qor.rpt:25"), "first": (0.004, "qor.rpt:11"),
                    "last": (-0.031, "qor.rpt:25"), "min": (-0.087, "qor.rpt:18"), "max": (0.004, "qor.rpt:11"),
                    "fails": (253.0, "qor.rpt:12"), "none": (None, rows["none"][1])}
    assert rows["none"][1].startswith("qor.rpt: no match")


@pytest.mark.parametrize("rule, value, verdict", [
    ("== 0", 0.0, "pass"), ("== 0", 3.0, "FAIL"), ("!= 0", 0.0, "FAIL"), ("<= -0.01", -0.02, "pass"),
    (">= 0", -0.001, "FAIL"), ("< 1e3", 999.0, "pass"), ("> 5", 5.0, "FAIL"), ("== 0", None, None), ("", 1.0, None)])
def test_a_pass_rule_gives_the_verdict(rule, value, verdict):
    assert Metric(name="m", pass_=rule).verdict(value) == verdict

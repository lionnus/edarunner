"""Tests of metrics.extract on the demo layout."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest
from helpers_results import POWER, POWER_NO_BLK, QOR, SUITE, TREE, area_hier, demo_qor, power_table

from edarunner.metrics import extract, extract_parameters
from edarunner.model import Limits, Metric, Parameter, Placement, Project, Safety, Site, Source, Stage, Sync, Task
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
    assert energy[0]["unit"] == "nJ" and energy[0]["canonical"] == ""
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


def test_scale_multiplies_each_value_at_extraction(tmp_path):
    run = results_tree(tmp_path / "results")
    for test in ("GEMM_M64_N64", "SOFTMAX_R197"):
        (run / "simulation/tests/demo" / test / "power/phases.json").write_text(json.dumps({"window_dur": 48213000000}))
    (run / "reports" / "3" / "area_hier.rpt").write_text(area_hier(1234.5))
    project = demo_project(tmp_path)
    project.metrics = {
        "window_ns": Metric(name="window_ns", stage=["power"], file="{task_dir}/power/phases.json", json="window_dur",
                            unit="ns", scale=1e-6),
        "area_mm2": Metric(name="area_mm2", stage=["synth"], step="3", file="reports/{step}/area_hier.rpt", area_hier=1,
                           unit="mm2", scale=1e-6),
    }
    rows = extract(project, RUN, tmp_path / "results", demo_tasks())
    assert {r["task"]: r["value"] for r in by_name(rows, "window_ns")} == {"k_small": 48213.0, "k_big": 48213.0}
    (area,) = by_name(rows, "area_mm2")
    assert area["value"] == pytest.approx(1234.5e-6)
    assert [(i["instance"], round(i["value"] * 1e6, 3)) for i in area["instances"]] == [("<top>", 1234.5), ("i_top", 1222.155)]


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


def test_a_star_in_the_file_pattern_globs_and_the_first_file_by_name_counts(tmp_path):
    run = results_tree(tmp_path / "results")
    for step, name, took in (("2", "b_route.log", 20), ("2", "a_route.log", 21), ("3", "c.log", 30), ("qor_data", "x.log", 9)):
        (run / "reports" / step).mkdir(exist_ok=True)
        (run / "reports" / step / name).write_text(f"took {took}\n")
    project = demo_project(tmp_path)
    project.metrics = {
        "took_s": Metric(name="took_s", stage=["synth"], step="*", file="reports/{step}/*.log", regex=r"took (\d+)"),
        "any_area": Metric(name="any_area", stage=["synth"], file="reports/*/area.rpt", regex=r"i_top (\S+)"),
    }
    rows = extract(project, RUN, tmp_path / "results", {})
    assert [(r["step"], r["value"], r["source_file"]) for r in by_name(rows, "took_s")] == [
        (2, 21.0, "reports/2/a_route.log:1"), (3, 30.0, "reports/3/c.log:1")]
    assert [(r["step"], r["value"], r["source_file"]) for r in by_name(rows, "any_area")] == [
        (None, 1000.0, "reports/0/area.rpt:1")]


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


def test_an_optional_metric_a_hook_without_a_number_and_a_where_with_placeholders(tmp_path):
    run = results_tree(tmp_path / "results")
    tests = run / "simulation" / "tests" / "demo"
    (tests / "GEMM_M64_N64" / "power" / "reports" / "power.csv").write_text(POWER)
    (tests / "SOFTMAX_R197" / "power" / "reports" / "power.csv").write_text(POWER_NO_BLK)
    (tests / "GEMM_M64_N64" / "power" / "phases.json").write_text(json.dumps({"window_ns": 3400, "trace": {"cycles": 2}}))
    (run / "bench.csv").write_text(SUITE)
    project = demo_project(tmp_path)
    (tmp_path / "hooks" / "trace.py").write_text("import json\n\ndef cycles(path):\n"
                                                 "    return json.load(open(path)).get('trace', {}).get('cycles')\n")
    blk = {"where": {"phase": "WHOLE", "instance": "u_blk_b"}, "column": "total_w"}
    power, phases = "{task_dir}/power/reports/power.csv", "{task_dir}/power/phases.json"
    project.metrics = {m.name: m for m in (
        Metric(name="blk_w", stage=["power"], file=power, csv=blk),
        Metric(name="blk_opt_w", stage=["power"], file=power, csv=blk, optional=True),
        Metric(name="cycles", stage=["power"], file="bench.csv", csv={"where": {"name": "{task.test}"}, "column": "cycles"}),
        Metric(name="trace_cycles", stage=["power"], file=phases, python="hooks/trace.py:cycles"),
        Metric(name="trace_opt", stage=["power"], file=phases, json="trace.cycles", optional=True),
        Metric(name="typ_opt", stage=["synth"], step="*", file="reports/{step}/qor.rpt", optional=True,
               regex=r"^Scenario\s+'func_typ'\n(?:.*\n)*?Critical Path Slack:\s+(\S+)"),
    )}
    rows = extract(project, RUN, tmp_path / "results", demo_tasks())
    assert {(r["name"], r["task"]): r["value"] for r in rows} == {
        ("blk_w", "k_small"): 0.02, ("blk_w", "k_big"): None, ("blk_opt_w", "k_small"): 0.02,
        ("cycles", "k_small"): 4100.0, ("cycles", "k_big"): 9800.0, ("trace_cycles", "k_small"): 2.0,
        ("trace_opt", "k_small"): 2.0}
    assert by_name(rows, "blk_w")[1]["source_file"] == (
        "simulation/tests/demo/SOFTMAX_R197/power/reports/power.csv: no row matches {'phase': 'WHOLE', 'instance': 'u_blk_b'}")
    assert by_name(rows, "cycles")[0]["source_file"] == "bench.csv"


def test_a_table_stores_the_rows_of_where_down_to_its_depth_and_takes_its_value_from_top(tmp_path):
    run = tmp_path / "results" / RUN_ID
    run.mkdir(parents=True)
    # 12 rows per part: the IDLE rows come first, so the WHOLE top is on line 14.
    (run / "inst.csv").write_text(power_table(TREE, {"IDLE": 0.01, "WHOLE": 0.25, "TRACE_0": 0.5}))
    (run / "leaf.csv").write_text("phase,instance,total_w\nWHOLE,top,1\nWHOLE,u_a,0.5\nWHOLE,u_a,0.4\n")
    table = {"instance": "full", "value": "total_w", "depth": "depth", "part": "phase", "where": {"phase": ["WHOLE", "TRACE_*"]},
             "top": {"phase": "WHOLE", "depth": "0"}, "max_depth": 2}
    project = demo_project(tmp_path)
    project.metrics = {m.name: m for m in (
        Metric(name="inst", stage=["synth"], file="inst.csv", table=table),
        Metric(name="slice", stage=["synth"], file="inst.csv",
               table={"instance": "full", "value": "total_w", "where": {"phase": "TRACE_0"}, "top": {"full": "chip", "phase": "TRACE_0"}}),
        Metric(name="leaf", stage=["synth"], file="leaf.csv", table={"instance": "instance", "value": "total_w", "top": {"instance": "top"}}),
        Metric(name="nolocal", stage=["synth"], file="inst.csv", table={**table, "local": "local_w"}),
        Metric(name="notop", stage=["synth"], file="inst.csv", table={**table, "top": {"phase": "BUSY"}}, optional=True),
    )}
    rows = {r["name"]: r for r in extract(project, RUN, tmp_path / "results", {})}
    assert (rows["inst"]["value"], rows["inst"]["source_file"]) == (0.25, "inst.csv:14")
    assert sorted((i["part"], i["depth"], i["instance"]) for i in rows["inst"]["instances"] if i["part"] == "WHOLE") == [
        ("WHOLE", 0, "chip"), ("WHOLE", 1, "chip/i_top"), ("WHOLE", 2, "chip/i_top/u_core"), ("WHOLE", 2, "chip/i_top/u_sum"),
        ("WHOLE", 2, "chip/i_top/u_vec")]
    assert {i["part"] for i in rows["inst"]["instances"]} == {"WHOLE", "TRACE_0"}
    # Without a part and a depth column, the part is empty and the depth counts the `/` of the path.
    assert rows["slice"]["value"] == 0.5 and {(i["part"], i["depth"]) for i in rows["slice"]["instances"]} == {("", d) for d in range(6)}
    assert rows["leaf"]["value"] is None and rows["leaf"]["source_file"].endswith("instance u_a repeats in part ''; name a column of unique paths")
    assert rows["nolocal"]["value"] is None and rows["nolocal"]["source_file"] == "inst.csv: no column local_w"
    assert "notop" not in rows


@pytest.mark.parametrize("rule, value, verdict", [
    ("== 0", 0.0, "pass"), ("== 0", 3.0, "FAIL"), ("!= 0", 0.0, "FAIL"), ("<= -0.01", -0.02, "pass"),
    (">= 0", -0.001, "FAIL"), ("< 1e3", 999.0, "pass"), ("> 5", 5.0, "FAIL"), ("== 0", None, None), ("", 1.0, None)])
def test_a_pass_rule_gives_the_verdict(rule, value, verdict):
    assert Metric(name="m", pass_=rule).verdict(value) == verdict


def test_parameters_from_the_head_of_a_log_a_report_a_json_object_and_a_hook(tmp_path):
    run = tmp_path / "results" / RUN_ID
    (run / "log").mkdir(parents=True)
    # The knob line is at the head of the log; a line after the head carries other values.
    (run / "log" / "synth.log").write_text("step 0 setup\nset ENABLE_X 1; set LANES 8;\n" + "." * 40 + "\nset LANES 2; set EXTRA 1;\n")
    (run / "reports").mkdir()
    (run / "reports" / "build.rpt").write_text("built at 12:00\ncommit: abc1234\n")
    (run / "settings.json").write_text(json.dumps({"flow": {"clock_ns": 1.0, "corner": "ss"}}))
    (tmp_path / "hooks").mkdir(exist_ok=True)
    (tmp_path / "hooks" / "k.py").write_text("def read(path):\n    return {'LANES': 99, 'MODE': 'fast'}\n\n\ndef bad(path):\n    return [1]\n")
    project = demo_project(tmp_path)
    knobs = Parameter(name="knobs", stage="synth", file="log/synth.log", regex=r"set (?P<key>\w+) (?P<value>[^;]+);",
                      head_bytes=48)
    project.parameters = {"knobs": knobs,
                          "source": Parameter(name="source", stage="synth", file="reports/build.rpt",
                                              regex=r"^commit:\s*(\S+)"),
                          "flow": Parameter(name="flow", stage="pnr", file="settings.json", json="flow"),
                          "hook": Parameter(name="hook", stage="synth", file="settings.json", python="hooks/k.py:read"),
                          "gone": Parameter(name="gone", stage="synth", file="reports/{label}.json", json="")}
    got, failed = extract_parameters(project, RUN, tmp_path / "results")
    # A key that two tables give keeps the first value, so the hook's LANES loses.
    assert got == {"ENABLE_X": "1", "LANES": "8", "source": "abc1234", "clock_ns": "1.0", "corner": "ss", "MODE": "fast"}
    assert failed == []
    knobs.head_bytes = 0  # the whole log: EXTRA comes in, and the first LANES still wins
    assert {k: v for k, v in extract_parameters(project, RUN, tmp_path / "results")[0].items() if k in ("LANES", "EXTRA")} == {
        "LANES": "8", "EXTRA": "1"}
    knobs.head_bytes = 20
    project.parameters["hook"].python = "hooks/k.py:bad"
    project.parameters["deep"] = Parameter(name="deep", stage="synth", file="settings.json", json="flow.corner.x")
    got, failed = extract_parameters(project, RUN, tmp_path / "results", stages=["synth"])
    assert got == {"source": "abc1234"} and failed == [
        ("", "parameters.knobs", "log/synth.log: no match for 'set (?P<key>\\\\w+) (?P<value>[^;]+);' in the first 20 bytes"),
        ("", "parameters.hook", "settings.json: a list, not an object of keys and values"),
        ("", "parameters.deep", "settings.json: no key flow.corner.x")]

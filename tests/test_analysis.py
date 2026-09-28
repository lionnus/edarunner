"""analysis.py and the commands over it: instances, the step of record, metrics per step, runtime, host and run samples."""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

import pytest
from helpers_cli import edr, seed
from helpers_results import TREE, area_hier, area_report, power_table

from edarunner import analysis, board, config, export, metrics
from edarunner.db import Database

SYNOPSYS = """\
Report : area
Total cell area:                   1000.000

Hierarchical area distribution
------------------------------

                                  Global cell area                  Local cell area
Hierarchical cell                 Absolute      Percent  Combi-       Noncombi-    Black-
                                  Total         Total    national     national     boxes         Design
--------------------------------  ------------  -------  -----------  -----------  ------------  ------
top_wrap                              1000.000    100.0       10.000        0.000         0.000  top_wrap
i_top                                  990.000     99.0        5.000        5.000         0.000  top_x
i_top/i_engine                         700.000     70.0      600.000      100.000         0.000  engine
i_top/i_a_very_long_instance_name_that_wraps_onto_the_next_line
                                       280.000     28.0      200.000       80.000         0.000  side
i_top/i_engine/i_lane                  100.000     10.0      100.000        0.000         0.000  lane
--------------------------------  ------------  -------  -----------  -----------  ------------  ------
Total                                                      815.000      185.000         0.000
"""

OPENROAD = """\
Design Area Summary
Hierarchical Area Report
--------------------------------------------------------------------------------
                 Global Area (um^2)          Global Instances          Local Area (um^2)          Local Instances
Hierarchy Name   Total StdCell Macro Cover Pad  Total StdCell Macro Cover Pad  Total StdCell Macro Cover Pad  Total StdCell Macro Cover Pad
--------------------------------------------------------------------------------
<top>            500.0 400.0 100.0 0.0 0.0  50 49 1 0 0  20.0 20.0 0.0 0.0 0.0  5 5 0 0 0
  i_soc          480.0 380.0 100.0 0.0 0.0  45 44 1 0 0  30.0 30.0 0.0 0.0 0.0  4 4 0 0 0
    gen_bank\\[0\\].i_sram 150.0 50.0 100.0 0.0 0.0  11 10 1 0 0  150.0 50.0 100.0 0.0 0.0  11 10 1 0 0
    i_core       300.0 300.0 0.0 0.0 0.0  30 30 0 0 0  300.0 300.0 0.0 0.0 0.0  30 30 0 0 0

Some other section
"""


def test_parse_synopsys_hierarchy_with_a_wrapped_name() -> None:
    rows = metrics.parse_area_hier(SYNOPSYS)
    assert [(r["instance"], r["depth"], r["value"]) for r in rows] == [
        ("<top>", 0, 1000.0), ("i_top", 1, 990.0), ("i_top/i_engine", 2, 700.0),
        ("i_top/i_a_very_long_instance_name_that_wraps_onto_the_next_line", 2, 280.0),
        ("i_top/i_engine/i_lane", 3, 100.0)]
    assert rows[2]["local"] == 700.0 and rows[2]["cells"] is None and {r["part"] for r in rows} == {""}


def test_parse_openroad_hierarchy_by_indentation() -> None:
    rows = metrics.parse_area_hier(OPENROAD)
    assert [(r["instance"], r["depth"], r["value"], r["local"], r["cells"]) for r in rows] == [
        ("<top>", 0, 500.0, 20.0, 50), ("i_soc", 1, 480.0, 30.0, 45),
        ("i_soc/gen_bank[0].i_sram", 2, 150.0, 150.0, 11), ("i_soc/i_core", 2, 300.0, 300.0, 30)]
    with pytest.raises(ValueError):
        metrics.parse_area_hier("Total cell area: 3\n")


def _area_metric(root: Path, depth: object = 2) -> None:
    toml = root / "edr.toml"
    toml.write_text(toml.read_text() + f"""
[metrics.area_hier_um2]
stage = ["synth", "pnr"]
step = "*"
file = "reports/{{step}}/area_hier.rpt"
area_hier = {json.dumps(depth)}
unit = "um2"
canonical = "design__instance__area"
""")


def _extract(root: Path, run_id: str, reports: dict[int, str]) -> None:
    for step, text in reports.items():
        d = root / "data" / "results" / run_id / "reports" / str(step)
        d.mkdir(parents=True, exist_ok=True)
        (d / "area_hier.rpt").write_text(text)
    project = config.load_project(root)
    project.metrics = {"area_hier_um2": project.metrics["area_hier_um2"]}
    with Database(project.data / "edr.db") as db:
        for row in metrics.extract(project, db.run(run_id), project.data / "results", {}):
            db.add_metric(row)


def test_instance_rows_filter_and_compare(demo: Path, capsys) -> None:
    _area_metric(demo)
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    _extract(demo, a, {2: SYNOPSYS, 3: SYNOPSYS})
    _extract(demo, b, {2: SYNOPSYS.replace("700.000", "630.000").replace("1000.000", "930.000")})
    with Database(demo / "data" / "edr.db") as db:
        assert {r["depth"] for r in db.instances()} == {0, 1, 2}  # area_hier = 2 keeps depth 2 at most
        assert db.metrics(name="area_hier_um2", step=2, run_ids=[a])[0]["value"] == 1000.0
    code, out, _ = edr(capsys, "--json", "metrics", "--source", "abc1234", "--instance", "i_top/i_engine")
    rows = json.loads(out)["data"]
    assert code == 0 and {r["instance"] for r in rows} == {"i_top/i_engine"}
    assert rows[0]["source_file"] == "reports/2/area_hier.rpt" and rows[0]["unit"] == "um2"
    # b has step 2 only, so both runs are compared at step 2, the last step both have.
    code, out, _ = edr(capsys, "compare", a, b, "--instances", "--depth", "2")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "area_hier_um2 in um2 at depth 2"
    engine = next(ln for ln in lines if ln.startswith("i_top/i_engine"))
    assert engine.split() == ["i_top/i_engine", "700", "630", "-70", "-10.0%"]
    assert next(ln for ln in lines if ln.startswith("<top>")).split()[-1] == "-7.0%"
    assert lines[1].split()[:7] == ["instance", "a", "(synth", "2)", "b", "(synth", "2)"]
    assert "a: reports/2/area_hier.rpt" in out
    code, out, _ = edr(capsys, "--json", "compare", a, b, "--instances", "--depth", "1")
    data = json.loads(out)["data"]
    assert [r["instance"] for r in data["rows"]] == ["i_top", "<sum>", "<other>", "<top>"]
    assert data["rows"][0]["delta"] == {b: 0.0} and data["rows"][2]["value"] == {a: 10.0, b: -60.0}
    # The report holds depth 3 at most, so depth 5 reads it and finds nothing.
    assert edr(capsys, "compare", a, b, "--instances", "--depth", "5")[1] == "no instance rows\n"
    with Database(demo / "data" / "edr.db") as db:
        last = analysis.last_areas(config.load_project(demo), db, [a, b, "nothing"], max_depth=1)
    assert set(last) == {a, b} and (last[a]["step"], last[b]["step"]) == (3, 2)
    assert last[a]["rows"] == [["<top>", 0, 1000.0], ["i_top", 1, 990.0]]


def _instances_project(root: Path) -> None:
    """ge_um2, an area metric that stores depth 3, and a table metric on the power CSV of each task: the whole window
    and the trace slices down to depth 3, the value from the top of the whole window."""
    toml = root / "edr.toml"
    toml.write_text(toml.read_text().replace("run_prefix =", "ge_um2 = 0.5\nrun_prefix =") + """
[metrics.area_hier_um2]
stage = "pnr"
step = "*"
file = "reports/{step}/area_hier.rpt"
area_hier = 3
unit = "um2"

[metrics.inst_w]
stage = "power"
file = "{task_dir}/power/reports/inst.csv"
table = { instance = "full", value = "total_w", depth = "depth", part = "phase", where = { phase = ["WHOLE", "TRACE_*"] }, top = { phase = "WHOLE", depth = "0" }, max_depth = 3 }
unit = "W"
""")


def _extract_files(root: Path, run_id: str, files: dict[str, str]) -> None:
    """Write `files` into the collected results of the run and extract its metrics with the task k_small done."""
    for rel, text in files.items():
        path = root / "data" / "results" / run_id / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    project = config.load_project(root)
    with Database(project.data / "edr.db") as db:
        for row in metrics.extract(project, db.run(run_id), project.data / "results", {"k_small": project.tasks["k_small"]}):
            db.add_metric(row)


def test_instances_side_by_side(demo: Path, capsys, tmp_path: Path) -> None:
    # beta lacks the adder u_add. The store keeps depth 3; the four lanes sit at depth 5.
    _instances_project(demo)
    alpha, beta = seed(demo, "alpha", "done"), seed(demo, "beta", "done")
    parts = {"WHOLE": 0.25, "TRACE_0": 0.5, "TRACE_1": 0.125, "IDLE": 0.01}
    for run, tree in ((alpha, TREE), (beta, [r for r in TREE if "u_add" not in r[0]])):
        _extract_files(demo, run, {"reports/5/area_hier.rpt": area_report(1024.0, tree),
                                   "simulation/tests/demo/GEMM_M64_N64/power/reports/inst.csv": power_table(tree, parts)})
    with Database(demo / "data" / "edr.db") as db:
        held = db.instances(run_ids=[alpha])
        top = db.metrics(run_ids=[alpha], name="inst_w")[0]
        assert export.export(config.load_project(demo), db, ["abc1234"], tmp_path / "exp")["ge_um2"] == 0.5
    assert max(r["depth"] for r in held) == 3 and {r["part"] for r in held} == {"", "WHOLE", "TRACE_0", "TRACE_1"}
    assert (top["value"], top["source_file"]) == (0.25, "simulation/tests/demo/GEMM_M64_N64/power/reports/inst.csv:2")

    def compare(*args: str, first: str = alpha) -> tuple[int, dict]:
        code, out, _ = edr(capsys, "--json", "compare", first, beta if first == alpha else alpha, "--instances", *args)
        data = json.loads(out)["data"]
        return code, {r["instance"]: r["value"][first] for r in data["rows"]} | {"": data}

    # The children and <other> add up to the top.
    code, rows = compare("--metric", "area_hier_um2", "--depth", "2")
    assert code == 0 and list(rows)[:-1] == ["i_top/u_core", "i_top/u_sum", "i_top/u_vec", "<sum>", "<other>", "<top>"]
    assert (rows["<sum>"], rows["<other>"], rows["<top>"]) == (896.0, 128.0, 1024.0)
    # The adder that beta lacks counts 0 there and shows its full delta, gone one way and new the other.
    for first, cells in ((alpha, ["64", "-", "-64", "gone"]), (beta, ["-", "64", "64", "new"])):
        out = edr(capsys, "compare", first, beta if first == alpha else alpha, "--instances", "--metric", "area_hier_um2",
                  "--depth", "3", "--instance", "i_top/u_sum/*")[1]
        assert next(ln for ln in out.splitlines() if ln.startswith("i_top/u_sum/u_add")).split()[1:] == cells
    # Depth 5 reads the report; a glob over the four lanes gives their sum, and kGE divides by ge_um2.
    code, rows = compare("--metric", "area_hier_um2", "--depth", "5", "--instance", "*/u_lane_*", "--unit", "kGE")
    data = rows.pop("")
    assert code == 0 and data["unit"] == "kGE" and "delta" not in data["runs"][0] and rows == pytest.approx(
        {**{f"i_top/u_vec/u_bank/u_lanes/u_lane_{k}": 0.064 for k in range(4)}, "<sum>": 0.256, "<other>": 1.792, "<top>": 2.048})
    # The table: the whole window by default, the part that `top` names.
    code, rows = compare("--metric", "inst_w", "--task", "k_small", "--depth", "2")
    assert code == 0 and rows.pop("")["part"] == "WHOLE" and rows == pytest.approx(
        {"chip/i_top/u_core": 0.125, "chip/i_top/u_sum": 0.0625, "chip/i_top/u_vec": 0.03125, "<sum>": 0.21875,
         "<other>": 0.03125, "<top>": 0.25})
    code, rows = compare("--metric", "inst_w", "--task", "k_small", "--part", "TRACE_1", "--depth", "3", "--instance", "*/u_add")
    assert code == 0 and rows["chip/i_top/u_sum/u_add"] == 0.0078125 and rows[""]["rows"][0]["delta"] == {beta: -0.0078125}
    # `where` stored no IDLE row, but the CSV holds them, and depth 4 reads it.
    assert "name one with --part: TRACE_0, TRACE_1, WHOLE" in edr(capsys, "compare", alpha, beta, "--instances", "--metric",
                                                              "inst_w", "--task", "k_small", "--part", "IDLE")[2]
    code, rows = compare("--metric", "inst_w", "--task", "k_small", "--part", "IDLE", "--depth", "4")
    assert code == 0 and rows["chip/i_top/u_vec/u_bank/u_lanes"] == pytest.approx(0.00125)
    # Two instance metrics need --metric, a task metric needs --task, and --part needs --instances.
    assert "name one instance metric with --metric: area_hier_um2, inst_w" in edr(capsys, "compare", alpha, beta, "--instances")[2]
    assert "name one with --task: k_small" in edr(capsys, "compare", alpha, beta, "--instances", "--metric", "inst_w")[2]
    assert "need --instances" in edr(capsys, "compare", alpha, beta, "--part", "WHOLE")[2]
    out = edr(capsys, "compare", alpha, beta, "--instances", "--metric", "area_hier_um2", "--csv")[1]
    assert out.splitlines()[:2] == ["instance,alpha (pnr 5),beta (pnr 5)", "i_top,960.0,960.0"]
    out = edr(capsys, "--json", "metrics", "--source", "abc1234", "--metric", "area_hier_um2", "--instance", "*/u_add",
              "--unit", "kGE")[1]
    assert [(r["label"], r["value"], r["unit"]) for r in json.loads(out)["data"]] == [("alpha", pytest.approx(0.128), "kGE")]


def test_extract_fills_the_area_rows_of_an_old_run(demo: Path, capsys) -> None:
    _area_metric(demo)
    a = seed(demo, "a", "done")
    _extract(demo, a, {2: SYNOPSYS})
    with Database(demo / "data" / "edr.db") as db:
        db.conn.execute("DELETE FROM instances")
        db.conn.commit()
    code, out, _ = edr(capsys, "extract", "a@demo")
    assert code == 0 and out == f"{a}: 0 new, 1 changed, 0 unchanged, 0 failed, 0 removed\n"
    with Database(demo / "data" / "edr.db") as db:
        assert {r["depth"] for r in db.instances()} == {0, 1, 2}
    assert edr(capsys, "extract", "a@demo")[1] == f"{a}: 0 new, 0 changed, 1 unchanged, 0 failed, 0 removed\n"


def test_area_hier_takes_a_depth_from_one(demo: Path) -> None:
    _area_metric(demo, True)
    with pytest.raises(config.ConfigError, match="area_hier"):
        config.load_project(demo)


def _metric(root: Path, run_id: str, name: str, step: int, value: float, stage: str = "synth") -> None:
    with Database(root / "data" / "edr.db") as db:
        db.add_metric({"run_id": run_id, "stage": stage, "step": step, "name": name, "value": value, "unit": "ns",
                       "source_file": f"reports/{step}/qor.rpt"})


def _steps(n: int) -> str:
    return "[" + ", ".join(f'"s{i}"' for i in range(n)) + "]"


def _record_project(root: Path) -> None:
    """pnr owns the steps 4 to 12 and export 13 to 15; the area metric records the deepest pnr step from 11 on."""
    toml = root / "edr.toml"
    text = toml.read_text().replace('steps = ["setup", "analyze", "elaborate", "synth", "cts", "route"]', f"steps = {_steps(13)}")
    toml.write_text(text.replace("[stages.export]\n", f"[stages.export]\nsteps = {_steps(16)}\n") + """
[metrics.area_hier_um2]
stage = ["pnr", "export"]
step = "*"
file = "reports/{step}/area_hier.rpt"
area_hier = 2
unit = "um2"
record = { stage = "pnr", from = 11 }
""")


def test_each_run_at_its_step_of_record(demo: Path, capsys, tmp_path: Path) -> None:
    # alpha ends route-opt at step 12 and exports at 15; beta ends at step 11 and leaves step 12 empty; gamma ends at 9.
    _record_project(demo)
    alpha, beta, gamma = seed(demo, "alpha", "done"), seed(demo, "beta", "done"), seed(demo, "gamma", "FAILED:pnr")
    _extract(demo, alpha, {9: area_hier(1000.0), 11: area_hier(1100.0), 12: area_hier(1200.0), 15: area_hier(1250.0)})
    _extract(demo, beta, {9: area_hier(3000.0), 11: area_hier(3300.0)})
    (demo / "data" / "results" / beta / "reports" / "12").mkdir()
    _extract(demo, gamma, {8: area_hier(900.0), 9: area_hier(950.0)})
    for run, steps in ((alpha, (9, 11, 12)), (beta, (9, 11))):
        for step in steps:
            _metric(demo, run, "wns_ns", step, -0.01 * step, stage="pnr")

    code, out, _ = edr(capsys, "compare", alpha, beta, "--instances", "--depth", "1")
    lines = out.splitlines()
    assert code == 0 and lines[1].split()[:7] == ["instance", "alpha", "(pnr", "12)", "beta", "(pnr", "11)"]
    assert next(ln for ln in lines if ln.startswith("<top>")).split() == ["<top>", "1200", "3300", "2100", "+175.0%"]
    code, out, _ = edr(capsys, "compare", alpha, beta, "--instances", "--depth", "0")
    assert code == 0 and [ln.split()[0] for ln in out.splitlines()].count("<top>") == 1
    # The area is at the step of record of each run; wns_ns has no `record`, so both runs are at step 11, which both have.
    code, out, _ = edr(capsys, "compare", alpha, beta, gamma)
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 2 and lines[2] == ["area_hier_um2", "1200", "(pnr", "12)", "3300", "(pnr", "11)", "-", "2100", "+175.0%",
                                      "-", "-"]
    assert lines[3][:7] == ["wns_ns", "-0.11", "(pnr", "11)", "-0.11", "(pnr", "11)"]
    assert out.splitlines()[-1] == "missing: gamma has pnr steps 8 to 9"
    code, out, _ = edr(capsys, "--json", "compare", alpha, gamma, "--instances")
    data = json.loads(out)["data"]
    assert code == 2 and [r["run_id"] for r in data["runs"]] == [alpha]
    assert data["missing"] == [{"run_id": gamma, "label": "gamma", "metric": "area_hier_um2", "task": "", "stage": "pnr",
                                "steps": [8, 9]}]
    code, out, _ = edr(capsys, "compare", alpha, beta, "--instances", "--step", "12")
    assert code == 2 and out.splitlines()[-1] == "missing: beta has pnr steps 9, 11"
    # Another stage than the record stage takes the deepest step the runs share there.
    code, out, _ = edr(capsys, "compare", alpha, beta, "--stage", "export")
    assert code == 0 and out.splitlines()[2].split() == ["area_hier_um2", "1250", "(export", "15)", "-", "-", "-"]

    project = config.load_project(demo)
    with Database(demo / "data" / "edr.db") as db:
        areas = analysis.last_areas(project, db, [alpha, beta, gamma])
        export.export(project, db, ["abc1234"], tmp_path / "exp")
    assert {rid: (a["stage"], a["step"]) for rid, a in areas.items()} == {alpha: ("pnr", 12), beta: ("pnr", 11)}
    with open(tmp_path / "exp" / "metrics.csv", newline="") as fh:
        marks = {(r["label"], r["stage"], int(r["step"])): r["record"] for r in csv.DictReader(fh) if r["metric"] == "area_hier_um2"}
    assert {k for k, v in marks.items() if v == "1"} == {("alpha", "pnr", 12), ("beta", "pnr", 11)}
    assert len(marks) == 8 and set(marks.values()) == {"0", "1"}


def test_compare_side_by_side_at_the_deepest_common_step(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    for run, wns in ((a, (-0.2, 0.1)), (b, (-0.1, -0.05))):
        _metric(demo, run, "wns_ns", 2, wns[0])
        _metric(demo, run, "wns_ns", 3, wns[1])
        _metric(demo, run, "cells", 3, 1000 if run == a else 1100)
    _metric(demo, a, "cells", 4, 1200, stage="pnr")
    # Without `record`, both runs are at the deepest step they share, 3; a percent across a sign change is left out.
    code, out, _ = edr(capsys, "compare", a, b)
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0] == ["metric", "task", "a", "b", "Δ", "b", "Δ", "%"]
    assert lines[2:] == [["cells", "1000", "(synth", "3)", "1100", "(synth", "3)", "100", "+10.0%"],
                         ["wns_ns", "0.1", "(synth", "3)", "-0.05", "(synth", "3)", "-0.15", "-"]]
    code, out, _ = edr(capsys, "compare", a, b, "--step", "2")
    assert code == 0 and out.splitlines()[2].split() == ["wns_ns", "-0.2", "(synth", "2)", "-0.1", "(synth", "2)", "0.1",
                                                         "+50.0%"]
    code, out, _ = edr(capsys, "--json", "compare", a, b, "--metric", "cells")
    rows = json.loads(out)["data"]["rows"]
    assert code == 0 and len(rows) == 1 and rows[0]["source_file"] == {a: "reports/3/qor.rpt", b: "reports/3/qor.rpt"}
    assert (rows[0]["stage"], rows[0]["step"]) == ({a: "synth", b: "synth"}, {a: 3, b: 3})
    assert edr(capsys, "compare", a, b, "--metric", "nothing")[0] == 2


def test_compare_names_the_sources_when_they_differ(demo: Path, capsys) -> None:
    dirty_tag = "abc1234-dirty-0badc0de"
    a, dirty = seed(demo, "a", "done"), seed(demo, "a", "done", source=dirty_tag)
    for run, wns in ((a, -0.2), (dirty, -0.1)):
        _metric(demo, run, "wns_ns", 3, wns)
    code, out, _ = edr(capsys, "compare", "a@abc1234", f"a@{dirty_tag}")
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"mixed sources: abc1234, {dirty_tag}"
    assert lines[1].split()[2:4] == ["a@abc1234", f"a@{dirty_tag}"]
    assert json.loads(edr(capsys, "--json", "compare", a, dirty)[1])["data"]["mixed_sources"] is True
    assert json.loads(edr(capsys, "--json", "compare", a, a)[1])["data"]["mixed_sources"] is False
    code, _, err = edr(capsys, "compare", "a@demo", a)
    assert code == 1 and "2 runs have label 'a' in batch 'demo'" in err
    twins = [{"run_id": "20261001_0900_a_demo_gaaa111", "label": "a", "source": "aaa111"},
             {"run_id": "20261002_0900_a_demo_gaaa111", "label": "a", "source": "aaa111"},
             {"run_id": "20261002_0900_b_demo_gaaa111", "label": "b", "source": "aaa111"}]
    assert list(analysis.names(twins).values()) == ["a 20261001", "a 20261002_0900_a", "b"]


def test_the_compare_page_holds_every_run_and_opens_on_the_runs_of_compare_html(demo: Path, capsys, tmp_path: Path) -> None:
    old, gone, live = seed(demo, "a", "done", batch="old"), seed(demo, "b", "done", state="retired"), seed(demo, "c", "done")
    with Database(demo / "data" / "edr.db") as db:
        db.mark_batch_retired("old")
    page = tmp_path / "page" / "compare.html"
    code, _, err = edr(capsys, "compare", "c@demo", "a@old", "--html", str(page))

    def block(name: str) -> object:
        return json.loads(page.read_text().split(f'id="edr-{name}">')[1].split("</script>")[0])

    # A retired run and a run of a retired batch are on the page with a mark.
    assert code == 2 and err.endswith(f"wrote {page}\n") and f'<script src="{board.PLOTLY_URL}">' in page.read_text()
    assert {r["run_id"]: r["retired"] for r in block("runs")} == {old: 1, gone: 1, live: 0}
    assert block("tick") == {"runs": [live, old], "named": True}
    assert block("better") == {"area_cell_um2": "lower", "wns_ns": "higher", "energy_nj": "lower"}
    (page.parent / board.PLOTLY_FILE).write_text("// a local copy\n")
    assert edr(capsys, "compare", live, "--html", str(page))[0] == 2
    assert f'<script src="{board.PLOTLY_FILE}">' in page.read_text() and block("tick") == {"runs": [live], "named": True}


def test_metrics_over_the_steps_of_one_run(demo: Path, capsys) -> None:
    a = seed(demo, "a", "done")
    _metric(demo, a, "wns_ns", 1, -0.3)
    _metric(demo, a, "wns_ns", 2, -0.1)
    _metric(demo, a, "area_um2", 2, 500.0)
    _metric(demo, a, "wns_ns", 4, 0.05, stage="pnr")
    code, out, _ = edr(capsys, "metrics", "--run", a, "--over", "steps")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0] == ["stage", "step", "name", "area_um2", "wns_ns"]
    assert lines[2:] == [["synth", "1", "analyze", "-", "-0.3"], ["synth", "2", "elaborate", "500", "-0.1"],
                         ["pnr", "4", "cts", "-", "0.05"]]
    code, out, _ = edr(capsys, "metrics", "--run", a, "--over", "steps", "--metric", "wns_ns")
    lines = [ln.split() for ln in out.splitlines()]
    assert lines[0] == ["stage", "step", "name", "wns_ns", "Δ", "source"]
    assert lines[4] == ["pnr", "4", "cts", "0.05", "0.15", "reports/4/qor.rpt"]
    assert edr(capsys, "metrics", "--over", "steps", "--source", "abc1234")[0] == 1
    assert edr(capsys, "metrics")[0] == 1


def test_a_pass_rule_marks_a_failing_value(demo: Path, capsys) -> None:
    # The demo's setup_violations has pass = "== 0".
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    for run, fails in ((a, (3, 0)), (b, (0, 0))):
        for step, n in zip((2, 3), fails):
            _metric(demo, run, "setup_violations", step, n)
            _metric(demo, run, "wns_ns", step, -0.1 if n else 0.1)
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234", "--metric", "setup_violations")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and ["a", "abc1234", "synth", "2", "setup_violations", "3", "FAIL", "ns", "reports/2/qor.rpt"] in lines
    assert ["a", "abc1234", "synth", "3", "setup_violations", "0", "ns", "reports/3/qor.rpt"] in lines
    code, out, _ = edr(capsys, "--json", "metrics", "--source", "abc1234", "--step", "2")
    assert {(m["label"], m["name"], m["verdict"]) for m in json.loads(out)["data"]} == {
        ("a", "setup_violations", "FAIL"), ("b", "setup_violations", "pass"), ("a", "wns_ns", None), ("b", "wns_ns", None)}
    code, out, _ = edr(capsys, "compare", a, b, "--metric", "setup_violations", "--step", "2")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and ["setup_violations", "3", "FAIL", "(synth", "2)", "0", "(synth", "2)", "-3", "-100.0%"] in lines
    code, out, _ = edr(capsys, "compare", a, b, "--metric", "setup_violations")
    assert ["setup_violations", "0", "(synth", "3)", "0", "(synth", "3)", "0", "-"] in [ln.split() for ln in out.splitlines()]
    code, out, _ = edr(capsys, "metrics", "--run", a, "--over", "steps")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0] == ["stage", "step", "name", "setup_violations", "wns_ns", "verdict"]
    assert lines[2:] == [["synth", "2", "elaborate", "3", "FAIL", "-0.1", "FAIL"], ["synth", "3", "synth", "0", "0.1", "pass"]]
    code, out, _ = edr(capsys, "metrics", "--run", a, "--over", "steps", "--metric", "wns_ns")
    assert code == 0 and "verdict" not in out


def test_status_shows_each_run_at_the_step_of_record_with_its_verdict(demo: Path, capsys) -> None:
    # The demo's area records the deepest pnr step from 5 on; setup_violations has pass = "== 0" and no `record`.
    alpha, beta = seed(demo, "alpha", "done"), seed(demo, "beta", "FAILED:pnr")
    gamma = seed(demo, "gamma", "done", source="def5678", batch="other")
    for run, area, fails in ((alpha, {3: 900, 4: 1000, 5: 1100}, {4: 2, 5: 0}), (beta, {3: 950, 4: 1050}, {4: 3}),
                             (gamma, {5: 1200}, {5: 1})):
        for step, v in area.items():
            _metric(demo, run, "area_cell_um2", step, v, stage="synth" if step < 4 else "pnr")
        for step, v in fails.items():
            _metric(demo, run, "setup_violations", step, v, stage="pnr")
    with Database(demo / "data" / "edr.db") as db:
        db.conn.execute("UPDATE metrics SET canonical='design__instance__area' WHERE name='area_cell_um2'")
        db.add_metric({"run_id": alpha, "stage": "power", "task": "k1", "name": "power_w", "value": 0.5})

    code, out, _ = edr(capsys, "status", "--source", "abc1234", "--metric", "area_cell_um2", "--metric", "setup_violations",
                       "--metric", "power_w")
    lines = out.splitlines()
    assert code == 0 and lines[0].split()[-3:] == ["area_cell_um2", "setup_violations", "power_w"]
    assert lines[2].split()[:2] == ["#1", "beta"] and lines[2].split()[-6:] == ["missing", "3", "FAIL", "(pnr", "4)", "-"]
    assert lines[3].split()[:2] == ["#2", "alpha"] and lines[3].split()[-7:] == ["1100", "(pnr", "5)", "0", "(pnr", "5)", "-"]
    assert lines[4:] == ["missing: beta@demo has pnr step 4"]
    code, out, _ = edr(capsys, "--json", "status", "--source", "abc1234", "--source", "def5678", "--metric",
                       "design__instance__area")
    data = json.loads(out)["data"]
    assert code == 0 and {r["run_id"] for r in data["runs"]} == {alpha, beta, gamma}
    assert data["metrics"][0]["metric"] == "area_cell_um2" and data["metrics"][0]["value"] == {alpha: 1100.0, gamma: 1200.0}
    assert data["missing"] == [{"run_id": beta, "label": "beta@demo", "metric": "area_cell_um2", "task": "", "stage": "pnr",
                                "steps": [4]}]
    code, out, err = edr(capsys, "status", "--metric", "setup_violations", "--metric", "area_cell_um2", "--csv")
    rows = list(csv.DictReader(out.splitlines()))
    assert err == "missing: beta@demo has pnr step 4\n" and rows[0]["area_cell_um2"] == ""
    assert code == 0 and [(r["label"], r["setup_violations"], r["setup_violations_stage"], r["setup_violations_step"],
                           r["setup_violations_verdict"]) for r in rows] == [
        ("beta", "3.0", "pnr", "4", "FAIL"), ("alpha", "0.0", "pnr", "5", "pass"), ("gamma", "1.0", "pnr", "5", "FAIL")]
    assert list(rows[0])[:7] == ["run_id", "label", "batch", "source", "host", "state", "phase"]
    code, _, err = edr(capsys, "status", "--narrow", "--metric", "area_cell_um2")
    assert code == 1 and "--narrow" in err
    with Database(demo / "data" / "edr.db") as db:
        db.add_metric({"run_id": alpha, "stage": "pnr", "step": 5, "name": "area_again", "canonical": "design__instance__area",
                       "value": 1100.0})
    code, _, err = edr(capsys, "status", "--metric", "design__instance__area")
    assert code == 1 and "area_again and area_cell_um2" in err


def test_runtime_of_one_run_and_of_a_batch(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    t0 = 1_790_000_000
    with Database(demo / "data" / "edr.db") as db:
        for run, k in ((a, 1), (b, 2)):
            db.upsert_stage_run({"run_id": run, "stage": "synth", "status": "done", "started": t0, "ended": t0 + 600 * k})
            db.upsert_stage_run({"run_id": run, "stage": "power", "status": "done", "started": t0 + 600 * k,
                                 "ended": t0 + 900 * k})
            db.upsert_stage_run({"run_id": run, "stage": "power", "task": "k1", "status": "done",
                                 "started": t0 + 600 * k, "ended": t0 + 700 * k})
        # A progress command that counts report directories sees each pnr step one step early.
        db.set_step_times(a, {"synth": {"1": t0 + 60, "0": t0, "2": t0 + 180}, "pnr": {"4": t0 + 990, "5": t0 + 1000}})
    # pnr writes one log per step; group 1 is the time, and the directory gives the step.
    toml = demo / "edr.toml"
    toml.write_text(toml.read_text().replace('[stages.pnr]\n', '[stages.pnr]\nstep_log = { file = "reports/{step}/*.log", '
                                             "regex = 'START (\\d+)' }\n"))
    reports = demo / "data" / "results" / a / "reports"
    for step, name, start, mtime in ((4, "cts", 1000, 1290), (5, "route", 1300, 1500), (6, "late", 9000, 9100)):
        (reports / str(step)).mkdir(parents=True)
        log = reports / str(step) / f"{name}.log"
        log.write_text(f"x\nSTART {t0 + start}\ny\nSTART {t0 + start + 50}\n")
        os.utime(log, (t0 + mtime, t0 + mtime))
    code, out, _ = edr(capsys, "runtime", a)
    lines = [ln.split() for ln in out.splitlines()]

    def at(s: int) -> list[str]:
        return board._ts(t0 + s).split()

    assert code == 0 and ["synth", "-", "attempt", "1,", "done", *at(0), "10m", "stage_runs"] in lines
    assert ["synth", "0", "setup", *at(0), "1m", "step_runs"] in lines
    assert ["synth", "2", "elaborate", *at(180), "7m", "step_runs"] in lines  # ends with the stage
    assert ["pnr", "4", "cts", *at(1000), "5m", "reports/4/cts.log:2"] in lines  # the next step ends it
    # Step 6 lies past the steps of pnr, so the mtime of its own file ends the last step of pnr.
    assert ["pnr", "5", "route", *at(1300), "3m", "reports/5/route.log:2"] in lines
    assert not [ln for ln in lines if ln[:2] == ["pnr", "6"]]
    assert ["power", "-", "1", "tasks,", "longest", "k1", "1m", "-", "1m", "stage_runs"] in lines
    assert lines[-1] == ["total", "-", "-", "-", "15m", "-"]
    code, out, _ = edr(capsys, "--json", "runtime", "--batch", "demo")
    data = json.loads(out)["data"]
    assert code == 0 and [d["total_s"] for d in data] == [900, 1800]
    code, out, _ = edr(capsys, "runtime", a, b)
    assert [ln.split()[-3:] for ln in out.splitlines()[2:]] == [["10m", "5m", "15m"], ["20m", "10m", "30m"]]
    assert edr(capsys, "runtime")[0] == 1


def test_runtime_marks_an_open_step_and_counts_a_live_stage_up_to_now(demo: Path, capsys) -> None:
    t0 = 1_790_000_000
    live, dead = seed(demo, "live", "stage:pnr"), seed(demo, "dead", "stage:pnr", state="dead", updated=t0 + 2000)
    imported = seed(demo, "imp", "done", tree=False, updated=t0 + 9000)
    with Database(demo / "data" / "edr.db") as db:
        for run in (live, dead):
            db.upsert_stage_run({"run_id": run, "stage": "synth", "status": "done", "started": t0, "ended": t0 + 600})
            db.upsert_stage_run({"run_id": run, "stage": "pnr", "status": "running", "started": t0 + 600})
            db.set_step_times(run, {"pnr": {"4": t0 + 600, "5": t0 + 1200}})
    toml = demo / "edr.toml"
    toml.write_text(toml.read_text().replace('[stages.pnr]\n', '[stages.pnr]\nstep_log = { file = "log/pnr.log", '
                                             "regex = 'START (\\d+)' }\n"))
    # One log for the whole run: the lines count from the first step of pnr, 4, and a later stage writes step 6.
    log = demo / "data" / "results" / imported / "log"
    log.mkdir(parents=True)
    (log / "pnr.log").write_text(f"START {t0 + 1000}\nSTART {t0 + 1300}\nSTART {t0 + 8000}\n")
    project = config.load_project(demo)
    with Database(demo / "data" / "edr.db") as db:
        rt = analysis.runtime(project, db, db.run(live), now=t0 + 3000)
        assert [(s["stage"], s["wall_s"], s["open"]) for s in rt["stages"]] == [("synth", 600, False), ("pnr", 2400, True)]
        assert [(s["step"], s["wall_s"], s["open"]) for s in rt["steps"]] == [(4, 600, False), (5, 1800, True)]
        assert (rt["total_s"], rt["open"]) == (3000, ["pnr 5 route"])
        rt = analysis.runtime(project, db, db.run(dead), now=t0 + 3000)  # a dead driver ends at its last heartbeat
        assert [(s["step"], s["wall_s"], s["open"]) for s in rt["steps"]] == [(4, 600, False), (5, 800, False)]
        assert (rt["total_s"], rt["open"]) == (2000, [])
    # The line of step 6 ends no step of pnr, and step 5 is not the last start of its file: it stays open.
    code, out, _ = edr(capsys, "runtime", imported)
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and ["pnr", "5", "route,", "open", *board._ts(t0 + 1300).split(), "-", "log/pnr.log:2"] in lines
    assert lines[-1] == ["total", "-", "at", "least,", "open:", "pnr", "5", "route", "-", "5m", "-"]
    code, out, _ = edr(capsys, "runtime", imported, dead)
    assert [ln.split()[3:] for ln in out.splitlines()[2:]] == [["-", "5m", "5m", "pnr", "5", "route"],
                                                                ["10m", "23m", "33m", "-"]]


def test_ingest_keeps_the_step_times_and_a_resume_replaces_one(tmp_path: Path) -> None:
    from edarunner import watch

    hb = {"run_id": "20260926_1200_a_demo_gabc1234", "phase": "stage:synth", "stage": "synth",
          "step_times": {"synth": {"0": 100, "1": 160}}}
    with Database(tmp_path / "edr.db") as db:
        watch.ingest(db, [("demo", hb)])
        watch.ingest(db, [("demo", {**hb, "step_times": {"synth": {"1": 400, "2": 500}}})])
        rows = [tuple(r) for r in db.conn.execute("SELECT stage, step, started FROM step_runs ORDER BY step")]
    assert rows == [("synth", 0, 100), ("synth", 1, 400), ("synth", 2, 500)]


def test_step_log_needs_file_and_a_regex_that_compiles(demo: Path) -> None:
    toml = demo / "edr.toml"
    text = toml.read_text()
    for bad, msg in (('{ file = "log/pnr.log" }', "needs file and regex"), ("""{ file = "x", regex = '(' }""", "regex")):
        toml.write_text(text.replace("[stages.pnr]\n", f"[stages.pnr]\nstep_log = {bad}\n"))
        with pytest.raises(config.ConfigError, match=msg):
            config.load_project(demo)


def _probe(load: float, free_ram: float) -> dict:
    return {"cores": 8, "load": load, "total_ram_gb": 64.0, "free_ram_gb": free_ram, "total_gb": 100.0,
            "free_gb": 40.0, "gpus": 0, "gpus_idle": 0}


def test_host_samples_history_and_the_status_chart(demo: Path, capsys) -> None:
    now = int(time.time())
    with Database(demo / "data" / "edr.db") as db:
        db.add_host_samples(now - 40 * 86400, {"h1": _probe(1, 60)})
        for k in range(4):
            db.add_host_samples(now - 3600 * (3 - k), {"h1": _probe(2 * k, 64 - 16 * k), "h2": {"error": "timeout"}})
        # 30 days are kept; a host that did not answer has no sample.
        assert [(r["host"], r["load"]) for r in db.host_samples(0)] == [("h1", 0), ("h1", 2), ("h1", 4), ("h1", 6)]
        samples = db.host_samples(now - 86400)
    code, out, _ = edr(capsys, "hosts", "--history")
    line = next(ln for ln in out.splitlines() if ln.startswith("h1"))
    assert code == 0 and "6/6 of 8" in line and "48/48 of 64" in line and "60/60 of 100" in line
    code, out, _ = edr(capsys, "--json", "hosts", "--history", "--since", "90m")
    rows = json.loads(out)["data"]
    assert [p[1] for p in rows[0]["cores_used"]] == [4, 6] and rows[0]["source"] == "host_samples"
    html = board.status_html([], [], {}, now, samples)
    assert html.count("<polyline") == 2 and "hosts, last day" in html and "<b>h1</b>" in html
    assert "hosts, last day" not in board.status_html([], [], {}, now, [])


def test_host_history_over_the_window_of_a_batch_with_its_own_use(demo: Path, capsys) -> None:
    t0 = int(time.time()) - 7200
    a = seed(demo, "a", "stage:pnr", host="h1", started=t0)  # it lives, so the window runs to now
    b = seed(demo, "b", "done", host="h1", started=t0 + 600, updated=t0 + 3000)
    seed(demo, "c", "done", batch="other", host="h2", started=t0, updated=t0 + 3600)
    with Database(demo / "data" / "edr.db") as db:
        for k in range(-2, 9):  # every ten minutes, from before the batch on
            db.add_host_samples(t0 + 600 * k, {"h1": _probe(4, 32), "h2": _probe(2, 48)})
        # A cycle keeps the heartbeats before the host probes, so the last sample of a live run comes first.
        for run, times, cpu in ((a, [*range(t0, t0 + 4800, 600), t0 + 4790], 200.0),
                                (b, range(t0 + 600, t0 + 3001, 600), 300.0)):
            for ts in times:
                db.add_run_sample({"run_id": run, "updated": ts, "cpu_pct": cpu, "rss_gb": 2.0, "tree_gb": 1.2})
    code, out, _ = edr(capsys, "--json", "hosts", "--history", "--batch", "demo")
    (h1,) = json.loads(out)["data"]  # h2 ran no run of the batch
    assert code == 0 and (h1["host"], h1["first"], h1["last"]) == ("h1", t0, t0 + 4800)
    use = h1["batch"]
    assert (use["batch"], use["runs"], use["source"]) == ("demo", 2, "run_samples")
    # b counts up to the host sample of its last heartbeat, and a up to the last host sample.
    assert [v for _, v in use["cores_used"]] == [2, 5, 5, 5, 5, 5, 2, 2, 2]
    assert [v for _, v in use["tree_gb"]] == [1.2, 2.4, 2.4, 2.4, 2.4, 2.4, 1.2, 1.2, 1.2]
    code, out, _ = edr(capsys, "hosts", "--batch", "demo")
    host, own = [ln for ln in out.splitlines() if ln.startswith(("h1", "  demo"))]
    assert "4/4 of 8" in host and "5/2 of 8" in own and "4/2 of 64" in own and "2/1 of 100" in own
    assert "an indented line is the batch's own use" in out
    # An ended batch ends at the last heartbeat of its runs; a run without samples draws no line of its own.
    (h2,) = json.loads(edr(capsys, "--json", "hosts", "--history", "--batch", "other")[1])["data"]
    assert (h2["host"], h2["first"], h2["last"]) == ("h2", t0, t0 + 3600) and "batch" not in h2
    code, _, err = edr(capsys, "hosts", "--history", "--batch", "nosuch")
    assert code == 1 and "no run of batch nosuch has started" in err


def test_ingest_keeps_one_sample_per_heartbeat_and_the_detail_shows_them(demo: Path, capsys) -> None:
    from edarunner import watch

    a = seed(demo, "a", "stage:synth")
    hb = {"run_id": a, "phase": "stage:synth", "updated": 1000, "cpu_pct": 350.0, "rss_gb": 2.5, "tree_gb": 1.0,
          "disk_free_gb": 90.0}
    with Database(demo / "data" / "edr.db") as db:
        for k in range(3):
            watch.ingest(db, [("demo", {**hb, "updated": 1000 + 60 * k, "tree_gb": 1.0 + k})])
        watch.ingest(db, [("demo", {**hb, "updated": 1120, "tree_gb": 9.0})])  # the same heartbeat again
        assert [s["tree_gb"] for s in db.run_samples(a)] == [1.0, 2.0, 3.0]
    code, out, _ = edr(capsys, "status", a)
    tree = next(ln for ln in out.splitlines() if ln.startswith("tree "))
    assert code == 0 and "from run_samples" in out and tree.split()[-4:] == ["3", "3", "GB", "3"]


def test_mlflow_export_without_mlflow_names_the_extra(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    seed(demo, "a", "done")
    monkeypatch.setitem(sys.modules, "mlflow", None)
    code, _, err = edr(capsys, "export", "--mlflow", str(tmp_path / "ml"))
    assert code == 1 and "edarunner[mlflow]" in err


def test_mlflow_export_writes_one_run_per_run(demo: Path, capsys, tmp_path: Path, monkeypatch) -> None:
    mlflow = pytest.importorskip("mlflow")  # CI installs the extra on one Python of the matrix
    a, b = seed(demo, "a", "done"), seed(demo, "b", "STOPPED")
    _metric(demo, a, "wns_ns", 2, -0.1)
    _metric(demo, a, "wns_ns", 3, 0.2)
    for step in (4, 5):  # the demo's area records the deepest pnr step from 5 on
        _metric(demo, a, "area_cell_um2", step, 1000.0 + step, stage="pnr")
    res = demo / "data" / "results" / a / "reports" / "3"
    res.mkdir(parents=True)
    (res / "qor.rpt").write_text("slack 0.2\n")
    (res / "big.rpt").write_bytes(b"x" * (2 << 20))  # over 1 MiB: not an artifact
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    code, out, _ = edr(capsys, "--json", "export", "--mlflow", str(tmp_path / "ml"))
    data = json.loads(out)["data"]
    assert code == 0 and [w["run_id"] for w in data["written"]] == [a, b]
    assert data["written"][0]["artifacts"] == 1
    client = mlflow.tracking.MlflowClient(tracking_uri=data["tracking_uri"])
    run = client.get_run(data["written"][0]["mlflow_run"])
    assert run.data.tags["edr.source"] == "abc1234" and run.data.params["config"] == "demo"
    assert [(m.step, m.value) for m in client.get_metric_history(run.info.run_id, "wns_ns")] == [(2, -0.1), (3, 0.2)]
    assert [(m.step, m.value) for m in client.get_metric_history(run.info.run_id, "record/area_cell_um2")] == [(5, 1005.0)]
    assert client.get_metric_history(run.info.run_id, "record/wns_ns") == []
    assert client.get_run(data["written"][1]["mlflow_run"]).info.status == "KILLED"
    code, out, _ = edr(capsys, "--json", "export", "--mlflow", str(tmp_path / "ml"))
    assert json.loads(out)["data"]["skipped"] == [a, b]
    assert edr(capsys, "export", "--source", "abc1234")[0] == 1


def test_compare_prints_the_parameters_that_differ(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    with Database(demo / "data" / "edr.db") as db:
        for run, netlist, source in ((a, 11, "abc1234"), (b, 15, "def5678")):
            db.set_parameters(run, {"config": "demo", "vars.netlist_stage": netlist}, "spec")
            db.set_parameters(run, {"source": source}, "checkout")  # never a line: the run names carry the source
        db.set_parameters(b, {"TCK": "1000"}, "spec")
    for run in (a, b):
        _metric(demo, run, "energy_nj", 3, 412.7 if run == a else 446.1)
    code, out, _ = edr(capsys, "compare", a, b)
    lines = [ln.split() for ln in out.splitlines()]
    # One line per parameter that differs, a run without the key shows -, then a blank line and the metrics.
    assert code == 0 and lines[0] == ["parameter", "a", "b"]
    assert lines[2:5] == [["TCK", "-", "1000"], ["vars.netlist_stage", "11", "15"], []]
    assert lines[5][:2] == ["metric", "task"] and lines[7][-2:] == ["33.4", "+8.1%"]
    data = json.loads(edr(capsys, "--json", "compare", a, b)[1])["data"]
    assert data["parameters"] == [{"key": "TCK", "value": {a: None, b: "1000"}},
                                  {"key": "vars.netlist_stage", "value": {a: "11", b: "15"}}]
    with Database(demo / "data" / "edr.db") as db:
        db.set_parameters(a, {"TCK": "1000", "vars.netlist_stage": "15"}, "spec")
    code, out, _ = edr(capsys, "compare", a, b, "--metric", "energy_nj")
    assert code == 0 and out.split()[:2] == ["metric", "task"]


def test_a_task_id_with_two_sets_of_fields_names_the_fields_that_differ_and_the_runs() -> None:
    rows = [{"run_id": f"r{i}", "task": "k_a", "key": "args", "value": "N=8"} for i in range(7)]
    rows += [{"run_id": f"r{i}", "task": "k_a", "key": "kernel", "value": "gemm"} for i in range(8)]
    rows += [{"run_id": "r7", "task": "k_a", "key": "limit", "value": "-4.0"}, {"run_id": "r7", "task": "k_b", "key": "args", "value": "N=8"},
             {"run_id": "r0", "task": "k_b", "key": "args", "value": "N=8"}]
    (clash,) = analysis.field_clashes(rows)
    assert clash["task"] == "k_a" and [s["runs"] for s in clash["sets"]] == [[f"r{i}" for i in range(7)], ["r7"]]
    assert analysis.clash_text(clash, {"r7": "b@x"}) == (
        'task k_a ran with 2 sets of fields: args="N=8" limit unset in r0, r1, r2, r3, r4 and 2 more; args unset limit="-4.0" in b@x')


def _sweep(root: Path) -> dict[str, str]:
    """A power sweep of the lane builds l4 to l32 and nolanes at abc1234, and l8 and l16 again at def5678, by
    label@source. l32, nolanes and l16.power, which continues l16, ran k_a only. The task k_b of l16 failed, the file of
    k_a did not parse for l8@def5678, which has no k_b value, and l16@def5678 stopped."""
    project, ids = config.load_project(root), {}
    watts = {"l4": 0.10, "l8": 0.12, "l16": 0.11, "nolanes": 0.09}
    for label, source, phase, tasks, energy in (
            ("l4", "abc1234", "done", "k_a k_b", (400.0, 900.0)), ("l8", "abc1234", "done", "k_a k_b", (380.0, 850.0)),
            ("l16", "abc1234", "done", "k_a k_b", (390.0,)), ("l32", "abc1234", "done", "k_a", (410.0,)),
            ("nolanes", "abc1234", "done", "k_a", (500.0,)), ("l8", "def5678", "done", "k_a k_b", (None,)),
            ("l16", "def5678", "STOPPED", "k_a k_b", (395.5,)), ("l16.power", "abc1234", "done", "k_a", (391.0,))):
        run = ids[f"{label}@{source}"] = seed(root, label, phase, source=source, tree=False)
        spec = project.state_dir / "demo" / f"{run}.spec.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(json.dumps({"stages": [{"name": "power", "tasks": [{"id": t} for t in tasks.split()]}]}))
        with Database(project.data / "edr.db") as db:
            for task, value in zip(("k_a", "k_b"), energy):
                db.add_metric({"run_id": run, "stage": "power", "task": task, "name": "energy_nj", "unit": "nJ",
                               "value": value, "source_file": f"simulation/tests/{label}/{task}/power/phases.json"
                               if value else "phases.json: no window_ns"})
            if source == "abc1234" and label in watts:
                db.add_metric({"run_id": run, "stage": "power", "task": "k_a", "name": "power_w", "unit": "W",
                               "value": watts[label], "source_file": f"simulation/tests/{label}/k_a/power/power.csv"})
    with Database(project.data / "edr.db") as db:
        db.upsert_stage_run({"run_id": ids["l16@abc1234"], "stage": "power", "task": "k_b", "status": "failed"})
    return ids


def test_a_pivot_gives_a_reason_for_each_empty_cell(demo: Path, capsys) -> None:
    ids = _sweep(demo)
    code, out, _ = edr(capsys, "metrics", "--source", "abc1234", "--source", "def5678", "--metric", "energy_nj", "--pivot")
    lines = [ln.split(None, 2) for ln in out.splitlines()]
    assert code == 0 and out.splitlines()[0] == "energy_nj in nJ" and out.splitlines()[1].split() == ["label", "source", "k_a", "k_b"]
    assert [[w, s, " ".join(cells.split())] for w, s, cells in lines[3:]] == [
        ["l4", "abc1234", "400 900"], ["l8", "abc1234", "380 850"], ["l8", "def5678", "failed no value"],
        ["l16", "abc1234", "390 failed"], ["l16", "def5678", "395.5 STOPPED"], ["l16.power", "abc1234", "391 not in job"],
        ["l32", "abc1234", "410 not in job"], ["nolanes", "abc1234", "500 not in job"]]
    data = json.loads(edr(capsys, "--json", "metrics", "--label", "l8", "--metric", "energy_nj", "--pivot")[1])["data"]
    assert data["columns"] == ["k_a", "k_b"] and [(r["run_id"], r["cells"]) for r in data["rows"]] == [
        (ids["l8@abc1234"], {"k_a": 380.0, "k_b": 850.0}), (ids["l8@def5678"], {"k_a": "failed", "k_b": "no value"})]
    out = edr(capsys, "metrics", "--source", "abc1234", "--metric", "energy_nj", "--pivot", "--csv")[1]
    assert out.splitlines()[:2] == ["label,source,k_a,k_b", "l4,abc1234,400.0,900.0"]
    code, _, err = edr(capsys, "metrics", "--source", "abc1234", "--pivot")
    assert code == 1 and "--pivot takes one metric; name it with --metric: energy_nj, power_w" in err


def test_where_is_a_number_across_every_source(demo: Path, capsys, tmp_path: Path) -> None:
    ids = _sweep(demo)
    assert edr(capsys, "export", "--source", "abc1234", "--out", str(tmp_path / "snap_abc"))[0] == 0
    # An older event names its directory relative to the project; a directory without its manifest holds no run.
    (tmp_path / "edr" / "old_def").mkdir()
    (tmp_path / "edr" / "old_def" / "manifest.json").write_text(json.dumps({"runs": [{"run_id": ids["l16@def5678"]}]}))
    with Database(demo / "data" / "edr.db") as db:
        db.add_event("user", "", "export", "def5678 -> ../old_def")
        db.add_event("user", "", "export", f"def5678 -> {tmp_path / 'gone'}")
    code, out, _ = edr(capsys, "metrics", "--label", "l16", "--task", "k_a", "--metric", "energy_nj")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0][-3:] == ["unit", "file", "snapshots"]
    assert lines[2:] == [
        ["l16.power", "abc1234", "power", "-", "k_a", "energy_nj", "391", "nJ", "simulation/tests/l16.power/k_a/power/phases.json",
         "../../snap_abc"],
        ["l16", "abc1234", "power", "-", "k_a", "energy_nj", "390", "nJ", "simulation/tests/l16/k_a/power/phases.json",
         "../../snap_abc"],
        ["l16", "def5678", "power", "-", "k_a", "energy_nj", "395.5", "nJ", "simulation/tests/l16/k_a/power/phases.json",
         "../old_def"]]
    rows = json.loads(edr(capsys, "--json", "metrics", "--task", "k_a", "--label", "l8")[1])["data"]
    assert [(r["source"], r["name"], r["snapshots"]) for r in rows] == [
        ("abc1234", "energy_nj", [str(tmp_path / "snap_abc")]), ("abc1234", "power_w", [str(tmp_path / "snap_abc")]),
        ("def5678", "energy_nj", [])]
    assert "failed: phases.json: no window_ns" in edr(capsys, "metrics", "--label", "l8", "--source", "def5678")[1]


def test_compare_ranks_the_runs_as_rows_against_a_ref_and_a_base(demo: Path, capsys) -> None:
    ids = _sweep(demo)
    code, out, _ = edr(capsys, "compare", "l4@abc1234", "l8@abc1234", "l16@abc1234", "--task", "k_a",
                       "--ref", "l4@abc1234", "--base", "nolanes@abc1234")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0] == ["run", "energy_nj[k_a]", "Δ", "%", "l4", "%", "nolanes", "rank",
                                      "power_w[k_a]", "Δ", "%", "l4", "%", "nolanes", "rank", "ranks"]
    assert lines[2:] == [["l4", "400", "-", "+0.0%", "-20.0%", "3", "0.1", "-", "+0.0%", "+11.1%", "1", "differ"],
                         ["l8", "380", "-20", "-5.0%", "-24.0%", "1", "0.12", "0.02", "+20.0%", "+33.3%", "3", "differ"],
                         ["l16", "390", "10", "-2.5%", "-22.0%", "2", "0.11", "-0.01", "+10.0%", "+22.2%", "2"]]
    data = json.loads(edr(capsys, "--json", "compare", "l8@abc1234", "l4@abc1234", "--task", "k_a", "--metric",
                          "energy_nj", "--base", "nolanes@abc1234")[1])["data"]
    (row,) = data["rows"]
    l4, l8 = ids["l4@abc1234"], ids["l8@abc1234"]
    assert (data["ref"], data["base"], data["ranks_differ"]) == (l8, ids["nolanes@abc1234"], [])
    assert (row["prev"], row["rank"], row["pct_base"]) == ({l8: None, l4: 20.0}, {l8: 1, l4: 2}, {l8: -24.0, l4: -20.0})
    code, _, err = edr(capsys, "compare", "l4@abc1234", "l8@abc1234", "--ref", "l4@abc1234")
    assert code == 1 and "--ref and --base need one value per metric of each run" in err

"""analysis.py and the commands over it: area, metrics per step, runtime, host and run samples."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edarunner import config, metrics
from edarunner.db import Database
from test_cli import demo, edr, seed  # noqa: F401  (the fixture and the helpers of test_cli)

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
    assert [(r["instance"], r["depth"], r["area"]) for r in rows] == [
        ("<top>", 0, 1000.0), ("i_top", 1, 990.0), ("i_top/i_engine", 2, 700.0),
        ("i_top/i_a_very_long_instance_name_that_wraps_onto_the_next_line", 2, 280.0),
        ("i_top/i_engine/i_lane", 3, 100.0)]
    assert rows[2]["local_area"] == 700.0 and rows[2]["cells"] is None


def test_parse_openroad_hierarchy_by_indentation() -> None:
    rows = metrics.parse_area_hier(OPENROAD)
    assert [(r["instance"], r["depth"], r["area"], r["local_area"], r["cells"]) for r in rows] == [
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


def test_area_rows_filter_and_compare(demo: Path, capsys) -> None:
    _area_metric(demo)
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    _extract(demo, a, {2: SYNOPSYS, 3: SYNOPSYS})
    _extract(demo, b, {2: SYNOPSYS.replace("700.000", "630.000").replace("1000.000", "930.000")})
    with Database(demo / "data" / "edr.db") as db:
        assert {r["depth"] for r in db.area()} == {0, 1, 2}  # area_hier = 2 keeps depth 2 at most
        assert db.metrics(name="area_hier_um2", step=2, run_ids=[a])[0]["value"] == 1000.0
    code, out, _ = edr(capsys, "--json", "metrics", "--design", "abc1234", "--instance", "i_top/i_engine")
    rows = json.loads(out)["data"]
    assert code == 0 and {r["instance"] for r in rows} == {"i_top/i_engine"}
    assert rows[0]["source_file"] == "reports/2/area_hier.rpt" and rows[0]["unit"] == "um2"
    # b has step 2 only, so both runs are compared at step 2, the last step both have.
    code, out, _ = edr(capsys, "compare", a, b, "--area", "--depth", "2")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "area um2 at depth 2"
    engine = next(ln for ln in lines if ln.startswith("i_top/i_engine"))
    assert engine.split() == ["i_top/i_engine", "700.0", "630.0", "-70.0", "-10.0%"]
    assert next(ln for ln in lines if ln.startswith("<top>")).split()[-1] == "-7.0%"
    assert "a: synth step 2, reports/2/area_hier.rpt" in out
    code, out, _ = edr(capsys, "--json", "compare", a, b, "--area", "--depth", "1")
    data = json.loads(out)["data"]
    assert [r["instance"] for r in data["rows"]] == ["i_top"] and data["rows"][0]["delta"] == {b: 0.0}
    assert edr(capsys, "compare", a, b, "--area", "--depth", "5")[0] == 2


def test_area_hier_takes_a_depth_from_one(demo: Path) -> None:
    _area_metric(demo, True)
    with pytest.raises(config.ConfigError, match="area_hier"):
        config.load_project(demo)


def _metric(root: Path, run_id: str, name: str, step: int, value: float, stage: str = "synth") -> None:
    with Database(root / "data" / "edr.db") as db:
        db.add_metric({"run_id": run_id, "stage": stage, "step": step, "name": name, "value": value, "unit": "ns",
                       "source_file": f"reports/{step}/qor.rpt"})


def test_compare_side_by_side_per_step(demo: Path, capsys) -> None:
    a, b = seed(demo, "a", "done"), seed(demo, "b", "done")
    for run, wns in ((a, (-0.2, 0.1)), (b, (-0.1, -0.05))):
        _metric(demo, run, "wns_ns", 2, wns[0])
        _metric(demo, run, "wns_ns", 3, wns[1])
        _metric(demo, run, "cells", 3, 1000 if run == a else 1100)
    code, out, _ = edr(capsys, "compare", a, b)
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and lines[0][:5] == ["stage", "step", "name", "task", "metric"]
    # Step names come from the stage's steps list; a percent across a sign change is left out.
    assert ["synth", "2", "elaborate", "wns_ns", "-0.2", "-0.1", "0.1", "+50.0%"] in lines
    assert ["synth", "3", "synth", "wns_ns", "0.1", "-0.05", "-0.15", "-"] in lines
    assert ["synth", "3", "synth", "cells", "1000", "1100", "100", "+10.0%"] in lines
    code, out, _ = edr(capsys, "--json", "compare", a, b, "--metric", "cells")
    rows = json.loads(out)["data"]["rows"]
    assert code == 0 and len(rows) == 1 and rows[0]["source_file"] == {a: "reports/3/qor.rpt", b: "reports/3/qor.rpt"}
    assert edr(capsys, "compare", a, b, "--metric", "nothing")[0] == 2


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
    assert edr(capsys, "metrics", "--over", "steps", "--design", "abc1234")[0] == 1
    assert edr(capsys, "metrics")[0] == 1

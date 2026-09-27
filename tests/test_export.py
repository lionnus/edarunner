"""export.py against a seeded database and a fake results tree in tmp_path."""

import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from edarunner import config, export
from edarunner.guards import Refuse
from edarunner.db import Database

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"

RUN_A_OLD = "20261001_0900_a_demo_gaaa111"
RUN_A = "20261002_1130_a_demo_gaaa111"
RUN_B = "20261002_1130_b_nodw_demo_gaaa111"
RUN_C = "20261003_0900_a_demo_gbbb222"

POWER_HIER = "phase,instance,total_w\nWHOLE,i_top,1.0\nWHOLE,i_top/i_core,0.8\nWHOLE,i_top/i_core/i_alu,0.3\n"
POWER_FLAT = "phase,total_w\nPHASE_A,0.100\nWHOLE,0.250\n"


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    project = config.load_project(root)
    db = Database(project.data / "edr.db")
    for run_id, label, src, phase, failed in (
        (RUN_A_OLD, "a", "aaa111", "done", 0),
        (RUN_A, "a", "aaa111", "done", 0),
        (RUN_B, "b_nodw", "aaa111", "stage:pnr", 0),
        (RUN_C, "a", "bbb222", "INCOMPLETE:1f0s", 1),
    ):
        db.upsert_run({"run_id": run_id, "batch": "demo", "label": label, "config": "demo", "src": src, "host": "local",
                        "phase": phase, "state": "running", "started": 100, "updated": 200,
                        "counts": {"done": 1, "failed": failed}})
    for run_id, value in ((RUN_A_OLD, 900.0), (RUN_A, 1000.0), (RUN_C, 2000.0)):
        db.add_metric({"run_id": run_id, "stage": "synth", "step": 3, "name": "area_cell_um2", "canonical": "design__instance__area",
                        "value": value, "unit": "um2", "source_file": "reports/3/area.rpt"})
    db.add_metric({"run_id": RUN_A, "stage": "power", "task": "k_small", "name": "power_w", "value": 0.25, "unit": "W"})
    results = project.data / "results"
    (results / RUN_A / "reports" / "3").mkdir(parents=True)
    (results / RUN_A / "reports" / "3" / "area.rpt").write_text("i_top 1000.0\n")
    (results / RUN_A / "sim" / "power" / "reports").mkdir(parents=True)
    (results / RUN_A / "sim" / "power" / "reports" / "power.csv").write_text(POWER_HIER)
    (results / RUN_A / "log").mkdir()
    (results / RUN_A / "log" / "synth.log").write_text("a long log\n")
    (results / RUN_A / "reports" / "3" / "run.log").write_text("another log\n")
    (results / RUN_B / "reports" / "0").mkdir(parents=True)
    (results / RUN_B / "reports" / "0" / "power.csv").write_text(POWER_FLAT)
    (results / RUN_C / "reports").mkdir(parents=True)
    (results / RUN_C / "reports" / "area.rpt").write_text("other design\n")
    yield project, db
    db.close()


def _read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_export_one_design(world, tmp_path):
    project, db = world
    out = tmp_path / "paper" / "export"
    manifest = export.export(project, db, "aaa111", out)

    assert [r["run_id"] for r in manifest["runs"]] == [RUN_A, RUN_B]
    assert manifest["runs"][0] == {"run_id": RUN_A, "label": "a", "config": "demo", "build_tag": None, "src": "aaa111",
                                   "host": "local", "phase": "done"}
    assert manifest["source"] == "aaa111" and manifest["project"] == "demo"
    assert manifest["schema"] == 1 and manifest["producer"].startswith("edarunner ")
    assert manifest["incomplete"] == [RUN_B]
    assert manifest["tables"] == {"runs.csv": 2, "metrics.csv": 2}
    assert json.loads((out / "manifest.json").read_text()) == manifest

    runs = _read_csv(out / "runs.csv")
    assert list(runs[0]) == export.RUN_COLUMNS
    assert [(r["run_id"], r["design"], r["ended"]) for r in runs] == [(RUN_A, "aaa111", "200"), (RUN_B, "aaa111", "")]

    metrics = _read_csv(out / "metrics.csv")
    assert list(metrics[0]) == export.METRIC_COLUMNS
    assert {m["run_id"] for m in metrics} == {RUN_A}
    area = next(m for m in metrics if m["metric"] == "area_cell_um2")
    assert (area["label"], area["stage"], area["step"], area["canonical"], area["value"], area["source"]) == (
        "a", "synth", "3", "design__instance__area", "1000.0", "reports/3/area.rpt")
    power = next(m for m in metrics if m["metric"] == "power_w")
    assert (power["task"], power["step"], power["unit"]) == ("k_small", "", "W")

    assert (out / "a" / "reports" / "3" / "area.rpt").read_text() == "i_top 1000.0\n"
    assert (out / "a" / "sim" / "power" / "reports" / "power.csv").read_text() == POWER_HIER
    assert (out / "b_nodw" / "reports" / "0" / "power.csv").read_text() == POWER_FLAT
    assert not (out / "a" / "reports" / "area.rpt").exists()
    assert not (out / "a" / "log").exists() and not (out / "a" / "reports" / "3" / "run.log").exists()

    on_disk = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} - {"manifest.json"}
    assert {f["path"] for f in manifest["files"]} == on_disk
    for f in manifest["files"]:
        data = (out / f["path"]).read_bytes()
        assert f["bytes"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert list(out.parent.iterdir()) == [out]


def test_labels_and_refusals(world, tmp_path):
    project, db = world
    manifest = export.export(project, db, "aaa111", tmp_path / "x", labels=["a"])
    assert [r["label"] for r in manifest["runs"]] == ["a"] and manifest["incomplete"] == []
    assert export.export(project, db, "bbb222", tmp_path / "y")["incomplete"] == [RUN_C]
    with pytest.raises(Refuse, match="not empty"):
        export.export(project, db, "aaa111", tmp_path / "x")
    with pytest.raises(Refuse, match="no run"):
        export.export(project, db, "zzz", tmp_path / "z")
    with pytest.raises(Refuse, match="no run"):  # a prefix of a source tag is not a match
        export.export(project, db, "aaa", tmp_path / "z")
    with pytest.raises(Refuse, match="no run"):
        export.export(project, db, "aaa111", tmp_path / "z", labels=["nope"])
    with pytest.raises(Refuse, match="empty design"):
        export.export(project, db, "", tmp_path / "z")
    assert not (tmp_path / "z").exists()


def test_dry_run_writes_nothing(world, tmp_path, capsys):
    project, db = world
    out = tmp_path / "paper" / "export"
    manifest = export.export(project, db, "aaa111", out, dry_run=True)
    assert not (tmp_path / "paper").exists()
    paths = [f["path"] for f in manifest["files"]]
    assert paths == ["runs.csv", "metrics.csv", "a/reports/3/area.rpt", "a/sim/power/reports/power.csv",
                     "b_nodw/reports/0/power.csv"]
    assert capsys.readouterr().out.splitlines() == paths
    real = export.export(project, db, "aaa111", out)
    assert [(f["path"], f["sha256"]) for f in manifest["files"]] == [(f["path"], f["sha256"]) for f in real["files"]]


def test_with_logs_copies_the_logs(world, tmp_path):
    project, db = world
    out = tmp_path / "with_logs"
    manifest = export.export(project, db, "aaa111", out, labels=["a"], with_logs=True)
    assert (out / "a" / "log" / "synth.log").read_text() == "a long log\n"
    assert (out / "a" / "reports" / "3" / "run.log").is_file()
    assert [f["path"] for f in manifest["files"]] == ["runs.csv", "metrics.csv", "a/log/synth.log",
                                                       "a/reports/3/area.rpt", "a/reports/3/run.log",
                                                       "a/sim/power/reports/power.csv"]

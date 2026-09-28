"""export.py against a seeded database and a fake results tree in tmp_path."""

import csv
import hashlib
import json
import shutil

import pytest

from edarunner import config, export
from edarunner.db import Database
from edarunner.guards import Refuse
from helpers_driver import DEMO

RUN_A_OLD = "20261001_0900_a_demo_gaaa111"
RUN_A = "20261002_1130_a_demo_gaaa111"
RUN_B = "20261002_1130_b_nodw_demo_gaaa111"
RUN_C = "20261003_0900_a_demo_gbbb222"
DIRTY_TAG = "ccc333-dirty-0badc0de"
CLEAN = "20261004_0800_x_demo_gccc333"
DIRTY = f"20261004_0800_x_demo_g{DIRTY_TAG}"

POWER_HIER = "phase,instance,total_w\nWHOLE,i_top,1.0\nWHOLE,i_top/i_core,0.8\nWHOLE,i_top/i_core/i_alu,0.3\n"
POWER_FLAT = "phase,total_w\nPHASE_A,0.100\nWHOLE,0.250\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # the state directory is under ~
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    project = config.load_project(root)
    db = Database(project.data / "edr.db")
    for run_id, label, source, phase, failed in (
        (RUN_A_OLD, "a", "aaa111", "done", 0),
        (RUN_A, "a", "aaa111", "done", 0),
        (RUN_B, "b_nodw", "aaa111", "stage:pnr", 0),
        (RUN_C, "a", "bbb222", "INCOMPLETE:1f0s", 1),
    ):
        db.upsert_run({"run_id": run_id, "batch": "demo", "label": label, "config": "demo", "source": source, "host": "local",
                        "phase": phase, "state": "running", "started": 100, "updated": 200,
                        "counts": {"done": 1, "failed": failed}})
    for run_id, value in ((RUN_A_OLD, 900.0), (RUN_A, 1000.0), (RUN_C, 2000.0)):
        db.add_metric({"run_id": run_id, "stage": "synth", "step": 3, "name": "area_cell_um2", "canonical": "design__instance__area",
                        "value": value, "unit": "um2", "source_file": "reports/3/area.rpt"})
    top = {"part": "WHOLE", "instance": "top", "depth": 0, "value": 0.25, "local": None, "cells": None}
    db.add_metric({"run_id": RUN_A, "stage": "power", "task": "k_small", "name": "power_w", "value": 0.25, "unit": "W",
                   "instances": [top, {**top, "instance": "top/u_a", "depth": 1, "value": 0.1}]})
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


def test_export_one_source(world, tmp_path):
    project, db = world
    config.save_json(project.state_dir / "demo" / f"{RUN_A}.spec.json",
                     {"record": {"edarunner": "9.9", "driver_sha256": "ab12", "tools": {"fc": "V-2023.12"}}})
    db.upsert_stage_run({"run_id": RUN_A, "stage": "synth", "status": "done", "started": 100, "ended": 150})
    db.set_task_fields(RUN_A, [("k_small", "args", "M=8 N=8", "spec"), ("k_small", "kernel", "gemm", "spec")])
    db.set_task_fields(RUN_A_OLD, [("k_small", "args", "M=4", "resolver")])
    out = tmp_path / "paper" / "export"
    manifest = export.export(project, db, ["aaa111"], out)

    assert [r["run_id"] for r in manifest["runs"]] == [RUN_A, RUN_B]
    record = manifest["runs"][0]["record"]
    assert {k: v for k, v in manifest["runs"][0].items() if k != "record"} == {"run_id": RUN_A, "label": "a", "config": "demo", "build_tag": None, "source": "aaa111",
                                   "host": "local", "phase": "done"}
    # The spec of RUN_A names the versions; RUN_B has no spec, so they are empty.
    assert record == {"host": "local", "started": 100, "ended": 200, "edarunner": "9.9", "driver_sha256": "ab12",
                      "tools": {"fc": "V-2023.12"}, "stages": [{"stage": "synth", "task": "", "attempt": 1,
                                                                 "status": "done", "started": 100, "ended": 150}],
                      "commands": []}
    assert manifest["runs"][1]["record"]["tools"] == {} and manifest["runs"][1]["record"]["ended"] is None
    assert manifest["sources"] == ["aaa111"] and manifest["project"] == "demo"
    assert manifest["schema"] == 3 and manifest["producer"].startswith("edarunner ")
    assert manifest["incomplete"] == [{"run_id": RUN_B, "label": "b_nodw", "source": "aaa111", "phase": "stage:pnr"}]
    assert manifest["skipped"] == [{"run_id": RUN_A_OLD, "label": "a", "source": "aaa111", "phase": "done"}]
    assert manifest["tables"] == {"runs.csv": 2, "metrics.csv": 2, "parameters.csv": 0, "task_fields.csv": 2, "instances.csv": 2}
    assert manifest["dirty_sources"] == [] and manifest["ge_um2"] is None
    assert json.loads((out / "manifest.json").read_text()) == manifest

    runs = _read_csv(out / "runs.csv")
    assert list(runs[0]) == export.RUN_COLUMNS
    assert [(r["run_id"], r["source"], r["ended"]) for r in runs] == [(RUN_A, "aaa111", "200"), (RUN_B, "aaa111", "")]

    metrics = _read_csv(out / "metrics.csv")
    assert list(metrics[0]) == export.METRIC_COLUMNS
    assert {m["run_id"] for m in metrics} == {RUN_A}
    area = next(m for m in metrics if m["metric"] == "area_cell_um2")
    assert (area["label"], area["host"], area["stage"], area["step"], area["canonical"], area["value"], area["source_file"]) == (
        "a", "local", "synth", "3", "design__instance__area", "1000.0", "reports/3/area.rpt")
    power = next(m for m in metrics if m["metric"] == "power_w")
    assert (power["task"], power["step"], power["unit"]) == ("k_small", "", "W")
    fields = _read_csv(out / "task_fields.csv")
    assert list(fields[0]) == export.TASK_FIELD_COLUMNS
    assert [tuple(f.values()) for f in fields] == [(RUN_A, "a", "aaa111", "k_small", "args", "M=8 N=8", "spec"),
                                                   (RUN_A, "a", "aaa111", "k_small", "kernel", "gemm", "spec")]
    instances = _read_csv(out / "instances.csv")
    assert list(instances[0]) == export.INSTANCE_COLUMNS
    assert [(i["run_id"], i["stage"], i["task"], i["metric"], i["part"], i["instance"], i["depth"], i["value"], i["unit"])
            for i in instances] == [(RUN_A, "power", "k_small", "power_w", "WHOLE", "top", "0", "0.25", "W"),
                                    (RUN_A, "power", "k_small", "power_w", "WHOLE", "top/u_a", "1", "0.1", "W")]

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
    manifest = export.export(project, db, ["aaa111"], tmp_path / "x", labels=["a"])
    assert [r["label"] for r in manifest["runs"]] == ["a"] and manifest["incomplete"] == []
    assert [r["run_id"] for r in export.export(project, db, ["bbb222"], tmp_path / "y")["incomplete"]] == [RUN_C]
    with pytest.raises(Refuse, match="not empty"):
        export.export(project, db, ["aaa111"], tmp_path / "x")
    with pytest.raises(Refuse, match="no run"):
        export.export(project, db, ["zzz"], tmp_path / "z")
    with pytest.raises(Refuse, match="no run"):  # a prefix of a source tag is not a match
        export.export(project, db, ["aaa"], tmp_path / "z")
    with pytest.raises(Refuse, match="no run"):
        export.export(project, db, ["aaa111"], tmp_path / "z", labels=["nope"])
    with pytest.raises(Refuse, match="empty source"):
        export.export(project, db, [""], tmp_path / "z")
    with pytest.raises(Refuse, match="empty source"):
        export.export(project, db, [], tmp_path / "z")
    assert not (tmp_path / "z").exists()


def test_dry_run_writes_nothing(world, tmp_path, capsys):
    project, db = world
    out = tmp_path / "paper" / "export"
    manifest = export.export(project, db, ["aaa111"], out, dry_run=True)
    assert not (tmp_path / "paper").exists()
    paths = [f["path"] for f in manifest["files"]]
    assert paths == ["runs.csv", "metrics.csv", "parameters.csv", "task_fields.csv", "instances.csv", "a/reports/3/area.rpt",
                     "a/sim/power/reports/power.csv", "b_nodw/reports/0/power.csv"]
    assert capsys.readouterr().out.splitlines() == paths
    real = export.export(project, db, ["aaa111"], out)
    assert [(f["path"], f["sha256"]) for f in manifest["files"]] == [(f["path"], f["sha256"]) for f in real["files"]]


def test_with_logs_copies_the_logs(world, tmp_path):
    project, db = world
    out = tmp_path / "with_logs"
    manifest = export.export(project, db, ["aaa111"], out, labels=["a"], with_logs=True)
    assert (out / "a" / "log" / "synth.log").read_text() == "a long log\n"
    assert (out / "a" / "reports" / "3" / "run.log").is_file()
    assert [f["path"] for f in manifest["files"]] == ["runs.csv", "metrics.csv", "parameters.csv", "task_fields.csv",
                                                       "instances.csv", "a/log/synth.log", "a/reports/3/area.rpt", "a/reports/3/run.log",
                                                       "a/sim/power/reports/power.csv"]


def test_an_export_of_two_sources_holds_one_run_per_label_and_source(world, tmp_path):
    project, db = world
    # One label twice in one batch: the clean run failed and started last, the dirty run ended done.
    for run_id, source, phase, started in ((CLEAN, "ccc333", "FAILED:pnr", 300), (DIRTY, DIRTY_TAG, "done", 200)):
        db.upsert_run({"run_id": run_id, "batch": "demo", "label": "x", "config": "demo", "source": source, "host": "local",
                       "phase": phase, "started": started, "updated": started + 50})
        (project.data / "results" / run_id / "reports").mkdir(parents=True)
        (project.data / "results" / run_id / "reports" / "area.rpt").write_text(f"{source}\n")
    clean = export.export(project, db, ["ccc333"], tmp_path / "clean")
    assert [r["run_id"] for r in clean["runs"]] == [CLEAN] and clean["skipped"] == []
    assert clean["incomplete"] == [{"run_id": CLEAN, "label": "x", "source": "ccc333", "phase": "FAILED:pnr"}]
    assert (tmp_path / "clean" / "x" / "reports" / "area.rpt").is_file()
    both = export.export(project, db, ["ccc333", DIRTY_TAG], tmp_path / "both")
    assert [r["run_id"] for r in both["runs"]] == [CLEAN, DIRTY] and both["sources"] == ["ccc333", DIRTY_TAG]
    assert [r["run_id"] for r in both["incomplete"]] == [CLEAN]
    assert (tmp_path / "both" / f"x@{DIRTY_TAG}" / "reports" / "area.rpt").read_text() == f"{DIRTY_TAG}\n"
    assert (tmp_path / "both" / "x@ccc333" / "reports" / "area.rpt").read_text() == "ccc333\n"


def test_parameters_run_columns_commands_and_the_diff_of_a_dirty_source(world, tmp_path):
    project, db = world
    db.upsert_run({"run_id": DIRTY, "batch": "old", "label": "x", "config": "demo", "source": DIRTY_TAG, "dirty": 1,
                   "host": "local", "phase": "done", "started": 200, "updated": 250, "tree_id": DIRTY})
    db.mark_batch_retired("old")
    db.set_parameters(DIRTY, {"config": "demo", "vars.netlist_stage": 15}, "spec")
    db.set_parameters(DIRTY, {"source": DIRTY_TAG, "nested.sub": "fed4321"}, "checkout")
    config.save_json(project.state_dir / "old" / f"{DIRTY}.spec.json", {"stages": [
        {"name": "synth", "cmd": "make synth"},
        {"name": "power", "tasks": [{"id": "k_small", "cmd": "make power NETLIST=out/15 TCK=1000"}]}]})
    kept = project.data / "sources" / DIRTY_TAG
    config.save_json(kept / "source.json", {"source": DIRTY_TAG, "base": "ccc333", "nested": {"sub": "fed4321"}})
    (kept / "source.diff").write_text("diff --git a/x b/x\n")
    out = tmp_path / "both"
    manifest = export.export(project, db, ["aaa111", DIRTY_TAG], out)

    assert manifest["dirty_sources"] == [{"source": DIRTY_TAG, "base": "ccc333", "nested": {"sub": "fed4321"},
                                          "diff_sha256": hashlib.sha256(b"diff --git a/x b/x\n").hexdigest()}]
    assert (out / "sources" / DIRTY_TAG / "source.diff").read_text() == "diff --git a/x b/x\n"
    assert f"sources/{DIRTY_TAG}/source.diff" in [f["path"] for f in manifest["files"]]
    record = next(r["record"] for r in manifest["runs"] if r["run_id"] == DIRTY)
    assert record["commands"] == [{"stage": "synth", "task": "", "cmd": "make synth"},
                                  {"stage": "power", "task": "k_small", "cmd": "make power NETLIST=out/15 TCK=1000"}]
    runs = {r["run_id"]: r for r in _read_csv(out / "runs.csv")}
    assert [runs[DIRTY][k] for k in ("batch", "dirty", "tree_id", "retired")] == ["old", "1", DIRTY, "1"]
    assert [runs[RUN_A][k] for k in ("batch", "dirty", "tree_id", "retired")] == ["demo", "", "", "0"]
    params = _read_csv(out / "parameters.csv")
    assert list(params[0]) == export.PARAMETER_COLUMNS and manifest["tables"]["parameters.csv"] == 4
    assert [(p["run_id"], p["label"], p["source"], p["key"], p["value"], p["origin"]) for p in params] == [
        (DIRTY, "x", DIRTY_TAG, "config", "demo", "spec"), (DIRTY, "x", DIRTY_TAG, "nested.sub", "fed4321", "checkout"),
        (DIRTY, "x", DIRTY_TAG, "source", DIRTY_TAG, "checkout"), (DIRTY, "x", DIRTY_TAG, "vars.netlist_stage", "15", "spec")]
    # A dirty source whose diff was never kept is listed with its base and without a sha256.
    db.upsert_run({"run_id": "20261005_0800_y_demo_gddd444-dirty", "batch": "old", "label": "y", "source": "ddd444-dirty",
                   "phase": "done"})
    lost = export.export(project, db, ["ddd444-dirty"], tmp_path / "lost")["dirty_sources"]
    assert lost == [{"source": "ddd444-dirty", "base": "ddd444", "nested": {}, "diff_sha256": None}]

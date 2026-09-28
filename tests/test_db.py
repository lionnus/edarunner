"""Tests of edarunner.db against a temporary database."""

import json
import os

import pytest

from edarunner import board
from edarunner import db as db_mod
from edarunner.db import Database, pick

RUN_A = "20261002_1130_a_demo_gaaa111"
RUN_B = "20261002_1130_b_nodw_demo_gaaa111"
RUN_C = "20261003_0900_a_demo_gbbb222"
DIRTY_TAG = "ccc333-dirty-0badc0de"
CLEAN = "20261004_0800_x_demo_gccc333"
DIRTY = f"20261004_0800_x_demo_g{DIRTY_TAG}"


def _seed(db: Database) -> None:
    db.upsert_batch({"batch": "demo", "project": "demo", "source": "aaa111", "run_date": "20261002_1130"})
    db.upsert_batch({"batch": "demo2", "project": "demo", "source": "bbb222", "run_date": "20261003_0900"})
    for run_id, batch, label, source in ((RUN_A, "demo", "a", "aaa111"), (RUN_B, "demo", "b_nodw", "aaa111"), (RUN_C, "demo2", "a", "bbb222")):
        db.upsert_run({"run_id": run_id, "batch": batch, "label": label, "config": "demo", "source": source, "host": "local",
                        "phase": "stage:synth", "state": "running", "counts": {"done": 0, "failed": 0}})


def test_schema_twice(tmp_path):
    path = tmp_path / "data" / "edr.db"
    with Database(path) as db:
        db.init_schema()
        db.add_event("user", None, "test", "first open")
    with Database(path) as db:
        assert len(db.events()) == 1
        tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"batches", "runs", "stage_runs", "parameters", "metrics", "artifacts", "events", "store"} <= tables
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_journal_mode_follows_the_filesystem(tmp_path, monkeypatch):
    with Database(tmp_path / "local.db") as db:
        assert (db.journal_mode, db.network_fs) == ("wal", None)
    monkeypatch.setattr(db_mod, "fs_magic", lambda path: 0x6969)
    with Database(tmp_path / "nfs.db") as db:
        assert (db.journal_mode, db.network_fs) == ("delete", "nfs")
        assert db.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    monkeypatch.setattr(db_mod, "fs_magic", lambda path: 0x12345678)
    assert db_mod.network_fs(tmp_path / "no" / "such" / "dir") is None


def test_upsert_then_update(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        _seed(db)
        db.upsert_run({"run_id": RUN_A, "phase": "done", "exit": 0, "counts": {"done": 2, "failed": 0}})
        row = db.run(RUN_A)
        assert row["phase"] == "done" and row["exit"] == 0
        assert row["label"] == "a" and row["host"] == "local"
        assert row["counts"] == {"done": 2, "failed": 0}
        raw = db.conn.execute("SELECT counts FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0]
        assert json.loads(raw) == {"done": 2, "failed": 0}
        assert db.run("nope") is None
        with pytest.raises(KeyError):
            db.upsert_run({"run_id": RUN_A, "colour": "red"})
        with pytest.raises(KeyError):
            db.upsert_run({"phase": "done"})

        db.upsert_stage_run({"run_id": RUN_A, "stage": "synth", "status": "running", "started": 1})
        db.upsert_stage_run({"run_id": RUN_A, "stage": "synth", "status": "done", "ended": 2, "exit": 0})
        db.upsert_stage_run({"run_id": RUN_A, "stage": "power", "task": "k_small", "status": "done"})
        rows = db.conn.execute("SELECT * FROM stage_runs ORDER BY stage").fetchall()
        assert [(r["stage"], r["task"], r["attempt"], r["status"], r["ended"]) for r in rows] == [
            ("power", "k_small", 1, "done", None), ("synth", "", 1, "done", 2)]

        db.set_parameters(RUN_A, {"DW": 0, "name": "x"}, "spec")
        db.set_parameters(RUN_A, {"DW": 1}, "import")
        db.set_parameters(RUN_A, {"DW": 2}, "import")
        # One row per key and origin; a second write under the same origin replaces the value.
        assert [(r["key"], r["value"], r["origin"]) for r in db.parameters(RUN_A)] == [
            ("DW", "2", "import"), ("DW", "0", "spec"), ("name", "x", "spec")]

        db.set_task_fields(RUN_A, [("k_old", "args", "M=4", "resolver")])
        db.set_task_fields(RUN_B, [("k_small", "args", "M=8", "spec")])
        # A second write replaces every task field of the run.
        db.set_task_fields(RUN_A, [("k_small", "kernel", "gemm", "import"), ("k_small", "args", "M=8", "import")])
        assert [(r["task"], r["key"], r["value"], r["origin"]) for r in db.task_fields([RUN_A])] == [
            ("k_small", "args", "M=8", "import"), ("k_small", "kernel", "gemm", "import")]
        assert [r["run_id"] for r in db.task_fields()] == [RUN_A, RUN_A, RUN_B]

        db.add_artifact({"run_id": RUN_A, "path": "reports/3/area.rpt", "bytes": 10, "class": "report"})
        db.add_artifact({"run_id": RUN_A, "path": "reports/3/area.rpt", "bytes": 12})
        assert db.conn.execute("SELECT bytes, class FROM artifacts").fetchall()[0][:] == (12, "report")

    with Database(tmp_path / "edr.db") as db:
        assert [r["run_id"] for r in db.runs(batch="demo")] == [RUN_A, RUN_B]
        assert [r["run_id"] for r in db.runs(state="running", batch="demo2")] == [RUN_C]


def test_resolve(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        _seed(db)
        assert db.resolve("a@demo") == RUN_A
        assert db.resolve("a@demo2") == RUN_C
        assert db.resolve("20261003") == RUN_C
        assert db.resolve(RUN_A) == RUN_A
        assert db.resolve("#2", [RUN_C, RUN_B]) == RUN_B
        with pytest.raises(KeyError, match="ambiguous") as exc:
            db.resolve("20261002_1130")
        assert RUN_A in str(exc.value) and RUN_B in str(exc.value)
        with pytest.raises(KeyError, match="no run id"):
            db.resolve("2027")
        with pytest.raises(KeyError, match="no run has label"):
            db.resolve("zzz@demo")
        with pytest.raises(KeyError, match="last board"):
            db.resolve("#3", [RUN_A])
        with pytest.raises(KeyError, match="last board"):
            db.resolve("#1", None)


def test_pick_takes_the_newest_done_run_by_start_time():
    first = {"run_id": "20261001_0900_x_demo_gaaa111", "phase": "done", "started": 300}
    later_id = {"run_id": "20261002_0900_x_demo_gaaa111", "phase": "done", "started": 200}
    failed = {"run_id": "20261003_0900_x_demo_gaaa111", "phase": "FAILED:pnr", "started": 400}
    assert pick([first, later_id, failed]) is first
    assert pick([later_id, {**first, "started": 200}]) is later_id  # the run id breaks a tie
    assert pick([failed, {**later_id, "phase": "STOPPED"}]) is failed  # nothing ended done: the newest run
    assert pick([]) is None


def test_label_at_batch_names_one_run_and_label_at_source_the_pick(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        _seed(db)
        # One label twice in one batch; the run ids sort against the start times.
        for run_id, source, phase, started in ((CLEAN, "ccc333", "FAILED:pnr", 300), (DIRTY, DIRTY_TAG, "done", 200)):
            db.upsert_run({"run_id": run_id, "batch": "twins", "label": "x", "config": "demo", "source": source,
                           "phase": phase, "started": started})
        with pytest.raises(KeyError, match="2 runs have label 'x' in batch 'twins'") as exc:
            db.resolve("x@twins")
        assert f"{CLEAN} (ccc333, FAILED:pnr)" in str(exc.value) and f"{DIRTY} ({DIRTY_TAG}, done)" in str(exc.value)
        assert db.resolve(f"x@{DIRTY_TAG}") == DIRTY and db.resolve("x@ccc333") == CLEAN and db.resolve("a@aaa111") == RUN_A
        runs = db.runs()
        assert [db.resolve(board.handle(r, runs)) for r in runs] == [r["run_id"] for r in runs]
        with pytest.raises(KeyError, match="no run has label 'x'"):
            db.resolve("x@aaa111")
        db.upsert_run({"run_id": "20261005_0900_y_demo_gddd444", "batch": "aaa111", "label": "y", "source": "ddd444"})
        with pytest.raises(KeyError, match="'aaa111' is both a batch and a source"):
            db.resolve("a@aaa111")


def test_metrics_by_source(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        _seed(db)
        for run_id, value in ((RUN_A, 1000.0), (RUN_B, 1010.0), (RUN_C, 2000.0)):
            assert db.add_metric({"run_id": run_id, "stage": "synth", "step": 3, "name": "area_cell_um2",
                                   "canonical": "design__instance__area", "value": value, "unit": "um2", "source_file": "reports/3/area.rpt"})
        assert db.add_metric({"run_id": RUN_A, "stage": "power", "task": "k_small", "name": "power_w", "value": 0.25})
        assert not db.add_metric({"run_id": RUN_A, "stage": "power", "task": "k_small", "name": "power_w", "value": 0.99})
        assert not db.add_metric({"run_id": RUN_A, "stage": "synth", "step": 3, "name": "area_cell_um2", "value": 1.0})
        assert db.conn.execute("SELECT count(*) FROM metrics").fetchone()[0] == 4

        rows = db.metrics(sources=["aaa111"])
        assert [(r["run_id"], r["name"], r["value"], r["label"]) for r in rows] == [
            (RUN_A, "power_w", 0.25, "a"), (RUN_A, "area_cell_um2", 1000.0, "a"), (RUN_B, "area_cell_um2", 1010.0, "b_nodw")]
        assert (rows[0]["host"], rows[0]["build_tag"]) == ("local", None)
        assert [r["value"] for r in db.metrics(sources=["bbb222"])] == [2000.0]
        assert len(db.metrics(sources=["aaa111", "bbb222"])) == 4 and db.metrics(sources=[]) == []
        assert [r["run_id"] for r in db.metrics(name="design__instance__area", step=3)] == [RUN_A, RUN_B, RUN_C]
        assert [r["run_id"] for r in db.metrics(stage="power")] == [RUN_A]
        assert [r["run_id"] for r in db.metrics(run_ids=[RUN_C])] == [RUN_C]
        assert db.metrics(run_ids=[]) == []


def test_a_failed_row_takes_a_value_and_a_removed_row_takes_its_instance_rows(tmp_path):
    top = {"instance": "<top>", "depth": 0, "part": "", "value": 5.0, "local": 1.0, "cells": None}
    with Database(tmp_path / "edr.db") as db:
        _seed(db)
        key = {"run_id": RUN_A, "stage": "synth", "step": 3, "name": "area_um2"}
        assert db.add_metric({**key, "value": None, "source_file": "reports/3/area.rpt: no match"})
        assert not db.add_metric({**key, "value": None, "source_file": "reports/3/area.rpt: another error"})
        assert db.add_metric({**key, "value": 5.0, "source_file": "reports/3/area.rpt",
                              "instances": [top, {**top, "instance": "u_a", "depth": 1, "value": 4.0}]})
        assert not db.add_metric({**key, "value": 6.0})
        assert [(m["value"], m["source_file"]) for m in db.metrics()] == [(5.0, "reports/3/area.rpt")]
        assert db.add_metric({**key, "value": 7.0, "instances": [{**top, "value": 7.0}]}, replace=True)
        assert [(a["instance"], a["value"]) for a in db.instances()] == [("<top>", 7.0)]
        db.remove_metrics([{**key, "task": ""}])
        assert db.metrics() == [] and db.instances() == []


def test_events(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        ids = [db.add_event("user", RUN_A if i % 2 else None, "note", f"e{i}") for i in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        assert [e["text"] for e in db.events(n=2)] == ["e3", "e4"]
        assert [e["text"] for e in db.events(run_id=RUN_A)] == ["e1", "e3"]
        assert [e["text"] for e in db.events(since_s=0)] == ["e0", "e1", "e2", "e3", "e4"]
        assert db.events(since_s=2**40) == []


def test_store(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        assert db.get_store("x") is None and db.get_store("x", {}) == {}
        db.set_store("x", {"a": [1, 2]})
        db.set_store("x", {"a": [1, 2, 3]})
        assert db.get_store("x") == {"a": [1, 2, 3]}
    with Database(tmp_path / "edr.db") as db:
        assert db.get_store("x") == {"a": [1, 2, 3]}


def test_board_json(tmp_path, monkeypatch):
    board = tmp_path / "data" / "board" / "board.json"
    with Database(tmp_path / "edr.db") as db:
        _seed(db)
        db.add_event("watch", RUN_A, "stale", "no heartbeat for 700 s")
        db.mark_batch_retired("demo2")
        db.write_board_json(board, {"local": {"free_cores": 4}})
        data = json.loads(board.read_text())
        assert [r["run_id"] for r in data["runs"]] == [RUN_A, RUN_B]
        assert data["runs"][0]["counts"] == {"done": 0, "failed": 0}
        assert data["events"][0]["kind"] == "stale"
        assert data["hosts"] == {"local": {"free_cores": 4}}
        assert not board.with_name("board.json.tmp").exists()
        assert [b["batch"] for b in db.batches() if b["retired"]] == ["demo2"]

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        db.add_event("user", RUN_B, "stop", "why not")
        with pytest.raises(OSError):
            db.write_board_json(board, {})
        assert json.loads(board.read_text()) == data
        assert not board.with_name("board.json.tmp").exists()

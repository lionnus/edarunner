"""Tests of edarunner.ledger against a temporary database."""

import json
import os

import pytest

from edarunner.ledger import Ledger

RUN_A = "20261002_1130_a_demo_gaaa111"
RUN_B = "20261002_1130_b_nodw_demo_gaaa111"
RUN_C = "20261003_0900_a_demo_gbbb222"


def _seed(led: Ledger) -> None:
    led.upsert_batch({"batch": "demo", "project": "demo", "source": "aaa111", "run_date": "20261002_1130"})
    led.upsert_batch({"batch": "demo2", "project": "demo", "source": "bbb222", "run_date": "20261003_0900"})
    for run_id, batch, label, src in ((RUN_A, "demo", "a", "aaa111"), (RUN_B, "demo", "b_nodw", "aaa111"), (RUN_C, "demo2", "a", "bbb222")):
        led.upsert_run({"run_id": run_id, "batch": batch, "label": label, "config": "demo", "src": src, "host": "local",
                        "phase": "stage:synth", "state": "running", "counts": {"done": 0, "failed": 0}})


def test_schema_twice(tmp_path):
    db = tmp_path / "data" / "edr.db"
    with Ledger(db) as led:
        led.init_schema()
        led.add_event("user", None, "test", "first open")
    with Ledger(db) as led:
        assert len(led.events()) == 1
        tables = {r[0] for r in led.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"batches", "runs", "stage_runs", "params", "metrics", "artifacts", "events", "kv"} <= tables
        assert led.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_upsert_then_update(tmp_path):
    with Ledger(tmp_path / "edr.db") as led:
        _seed(led)
        led.upsert_run({"run_id": RUN_A, "phase": "done", "exit": 0, "counts": {"done": 2, "failed": 0}})
        row = led.run(RUN_A)
        assert row["phase"] == "done" and row["exit"] == 0
        assert row["label"] == "a" and row["host"] == "local"
        assert row["counts"] == {"done": 2, "failed": 0}
        raw = led.db.execute("SELECT counts FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0]
        assert json.loads(raw) == {"done": 2, "failed": 0}
        assert led.run("nope") is None
        with pytest.raises(KeyError):
            led.upsert_run({"run_id": RUN_A, "colour": "red"})
        with pytest.raises(KeyError):
            led.upsert_run({"phase": "done"})

        led.upsert_stage_run({"run_id": RUN_A, "stage": "synth", "status": "running", "started": 1})
        led.upsert_stage_run({"run_id": RUN_A, "stage": "synth", "status": "done", "ended": 2, "exit": 0})
        led.upsert_stage_run({"run_id": RUN_A, "stage": "power", "task": "k_small", "status": "done"})
        rows = led.db.execute("SELECT * FROM stage_runs ORDER BY stage").fetchall()
        assert [(r["stage"], r["task"], r["attempt"], r["status"], r["ended"]) for r in rows] == [
            ("power", "k_small", 1, "done", None), ("synth", "", 1, "done", 2)]

        led.set_params(RUN_A, {"DW": 0, "name": "x"}, "config")
        led.set_params(RUN_A, {"DW": 1}, "override")
        params = {r["key"]: (r["value"], r["source"]) for r in led.db.execute("SELECT * FROM params")}
        assert params == {"DW": ("1", "override"), "name": ("x", "config")}

        led.add_artifact({"run_id": RUN_A, "path": "reports/3/area.rpt", "bytes": 10, "class": "report"})
        led.add_artifact({"run_id": RUN_A, "path": "reports/3/area.rpt", "bytes": 12})
        assert led.db.execute("SELECT bytes, class FROM artifacts").fetchall()[0][:] == (12, "report")

    with Ledger(tmp_path / "edr.db") as led:
        assert [r["run_id"] for r in led.runs(batch="demo")] == [RUN_A, RUN_B]
        assert [r["run_id"] for r in led.runs(state="running", batch="demo2")] == [RUN_C]


def test_resolve(tmp_path):
    with Ledger(tmp_path / "edr.db") as led:
        _seed(led)
        assert led.resolve("a@demo") == RUN_A
        assert led.resolve("a@demo2") == RUN_C
        assert led.resolve("20261003") == RUN_C
        assert led.resolve(RUN_A) == RUN_A
        assert led.resolve("#2", [RUN_C, RUN_B]) == RUN_B
        with pytest.raises(KeyError, match="ambiguous") as exc:
            led.resolve("20261002_1130")
        assert RUN_A in str(exc.value) and RUN_B in str(exc.value)
        with pytest.raises(KeyError, match="no run id"):
            led.resolve("2027")
        with pytest.raises(KeyError, match="no run has label"):
            led.resolve("zzz@demo")
        with pytest.raises(KeyError, match="last board"):
            led.resolve("#3", [RUN_A])
        with pytest.raises(KeyError, match="last board"):
            led.resolve("#1", None)


def test_metrics_by_design(tmp_path):
    with Ledger(tmp_path / "edr.db") as led:
        _seed(led)
        for run_id, value in ((RUN_A, 1000.0), (RUN_B, 1010.0), (RUN_C, 2000.0)):
            assert led.add_metric({"run_id": run_id, "stage": "synth", "step": 3, "name": "area_cell_um2",
                                   "canonical": "area.cell", "value": value, "unit": "um2", "source_file": "reports/3/area.rpt"})
        assert led.add_metric({"run_id": RUN_A, "stage": "power", "task": "k_small", "name": "power_w", "value": 0.25})
        assert not led.add_metric({"run_id": RUN_A, "stage": "power", "task": "k_small", "name": "power_w", "value": 0.99})
        assert not led.add_metric({"run_id": RUN_A, "stage": "synth", "step": 3, "name": "area_cell_um2", "value": 1.0})
        assert led.db.execute("SELECT count(*) FROM metrics").fetchone()[0] == 4

        rows = led.metrics(design="aaa111")
        assert [(r["run_id"], r["name"], r["value"], r["label"]) for r in rows] == [
            (RUN_A, "power_w", 0.25, "a"), (RUN_A, "area_cell_um2", 1000.0, "a"), (RUN_B, "area_cell_um2", 1010.0, "b_nodw")]
        assert [r["value"] for r in led.metrics(design="bbb222")] == [2000.0]
        assert [r["run_id"] for r in led.metrics(name="area.cell", step=3)] == [RUN_A, RUN_B, RUN_C]
        assert [r["run_id"] for r in led.metrics(stage="power")] == [RUN_A]
        assert [r["run_id"] for r in led.metrics(run_ids=[RUN_C])] == [RUN_C]
        assert led.metrics(run_ids=[]) == []


def test_events(tmp_path):
    with Ledger(tmp_path / "edr.db") as led:
        ids = [led.add_event("user", RUN_A if i % 2 else None, "note", f"e{i}") for i in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        assert [e["text"] for e in led.events(n=2)] == ["e3", "e4"]
        assert [e["text"] for e in led.events(run_id=RUN_A)] == ["e1", "e3"]
        assert [e["text"] for e in led.events(since_s=0)] == ["e0", "e1", "e2", "e3", "e4"]
        assert led.events(since_s=2**40) == []


def test_kv(tmp_path):
    with Ledger(tmp_path / "edr.db") as led:
        assert led.get_kv("x") is None and led.get_kv("x", {}) == {}
        led.set_kv("x", {"a": [1, 2]})
        led.set_kv("x", {"a": [1, 2, 3]})
        assert led.get_kv("x") == {"a": [1, 2, 3]}
    with Ledger(tmp_path / "edr.db") as led:
        assert led.get_kv("x") == {"a": [1, 2, 3]}


def test_board_json(tmp_path, monkeypatch):
    board = tmp_path / "data" / "board" / "board.json"
    with Ledger(tmp_path / "edr.db") as led:
        _seed(led)
        led.add_event("watch", RUN_A, "stale", "no heartbeat for 700 s")
        led.mark_batch_retired("demo2")
        led.write_board_json(board, {"local": {"free_cores": 4}})
        data = json.loads(board.read_text())
        assert [r["run_id"] for r in data["runs"]] == [RUN_A, RUN_B]
        assert data["runs"][0]["counts"] == {"done": 0, "failed": 0}
        assert data["events"][0]["kind"] == "stale"
        assert data["hosts"] == {"local": {"free_cores": 4}}
        assert not board.with_name("board.json.tmp").exists()
        assert [b["batch"] for b in led.batches() if b["retired"]] == ["demo2"]

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        led.add_event("user", RUN_B, "stop", "why not")
        with pytest.raises(OSError):
            led.write_board_json(board, {})
        assert json.loads(board.read_text()) == data
        assert not board.with_name("board.json.tmp").exists()

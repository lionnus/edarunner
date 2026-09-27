"""Rows shaped like the database `runs` table, for the board and the Telegram format tests."""

import json

NOW = 1_800_000_000
SRC = "gaaa111"
RUN = {
    "dead": "20261002_1130_a_demo_" + SRC,
    "stale": "20261002_1130_b_nodw_demo_" + SRC,
    "run1": "20261002_1130_c_demo_" + SRC,
    "run2": "20261002_1130_d_demo_" + SRC,
    "done": "20261001_0900_a_demo_" + SRC,
    "fail": "20261001_0900_b_nodw_demo_" + SRC,
}


def board_row(key, label, phase, state=None, age=10, **extra):
    row = {"run_id": RUN[key], "batch": "demo", "label": label, "config": "demo", "build_tag": "demo", "src": SRC,
           "dirty": 0, "host": "local", "root": "/tmp/edr-demo/x/edr/demo/" + RUN[key], "created": NOW - 7200,
           "phase": phase, "state": state, "stage": "synth", "step": 3, "exit": None, "killed_by": None,
           "started": NOW - 7200, "updated": NOW - age, "disk_free_gb": 100.0, "tree_gb": 1.5,
           "counts": json.dumps({"done": 1, "failed": 0, "skipped": 0, "running": 1, "queued": 0})}
    row.update(extra)
    return row


def board_rows():
    return [
        board_row("done", "a", "done", "running", age=3600, exit=0, counts={"done": 2, "failed": 0}),
        board_row("run1", "c", "stage:synth", "running"),
        board_row("fail", "b_nodw", "INCOMPLETE:1f0s", "running", age=7000, exit=8, stage="power", step=None,
             counts=json.dumps({"done": 1, "failed": 1})),
        board_row("run2", "d" * 40, "group:power_" * 3, None, host="hostB-long-name", batch="sweep10_long_name"),
        board_row("stale", "b_nodw", "stage:pnr", "stale", age=900),
        board_row("dead", "a", "stage:synth", "dead", age=5000),
    ]

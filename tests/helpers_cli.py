"""The runner of `edr` in-process and the seeded runs of the command line tests; `demo` is in conftest.py."""

from __future__ import annotations

import getpass
import json
import subprocess
import time
from pathlib import Path

from edarunner import board, cli, config
from edarunner.db import Database

DATE = "20260926_1200"


def edr(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def dead_pid() -> int:
    p = subprocess.Popen(["sleep", "0"])
    p.wait()
    return p.pid


def bdir(root: Path, batch: str = "demo") -> Path:
    """The batch directory of the state: <HOME>/.edr/<project>/<batch>."""
    return Path.home() / ".edr" / "demo" / batch


def seed(root: Path, label: str, phase: str | None, pid: int | None = None, batch: str = "demo",
         source: str = "abc1234", date: str = DATE, tree: bool = True, **extra) -> str:
    """A database row, a heartbeat and a run tree under the tmp scratch; returns the run id."""
    run_id = f"{date}_{label}_demo_g{source}"
    project = config.load_project(root)
    now = int(time.time())
    row = {"run_id": run_id, "batch": batch, "label": label, "config": "demo", "source": source, "host": "local",
           "phase": phase, "state": "running", "started": now - 100, "updated": now - 5,
           "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 0, "queued": 0}, **extra}
    if tree:
        run_root = Path(project.site.scratch[0]) / getpass.getuser() / "edr" / "demo" / run_id
        (run_root / "out" / "11").mkdir(parents=True)
        (run_root / "out" / "11" / "netlist.v").write_text("module top; endmodule\n")
        (run_root / "log").mkdir()
        row["root"] = str(run_root)
        terminal = str(phase).startswith(board.TERMINAL)
        hb = {**row, "driver_pid": pid, "pgids": [], "stage": "synth", "step": 2, "step_name": "elaborate",
              "tasks": {}, "exit": 0 if terminal else None, "last_log": "step 2 elaborate"}
        (project.state_dir / batch).mkdir(parents=True, exist_ok=True)
        (project.state_dir / batch / f"{run_id}.json").write_text(json.dumps(hb))
    with Database(project.data / "edr.db") as db:
        db.upsert_batch({"batch": batch, "project": "demo", "source": source})
        db.upsert_run(row)
    return run_id

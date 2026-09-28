"""Fake heartbeats in a tmp state dir, host local, a fake ssh and a recording notifier; `env` is in conftest.py."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from edarunner import watch
from edarunner.config import load_project
from edarunner.db import Database
from edarunner.hosts import HostProbe, Ssh
from edarunner.notify import Notifier
from helpers_driver import DEMO

NOW = 1_800_000_000.0
OLD, NEW = "20260926_1200", "20260926_1300"


def rid(label: str, date: str = OLD, source: str = "gabc1234") -> str:
    return f"{date}_{label}_demo_{source}"


class FakeSsh(Ssh):
    """Real local commands; canned probe, pid check, tool processes and kills."""

    def __init__(self, site) -> None:
        super().__init__(site)
        self.alive: dict[int, bool] = {}
        self.procs: list[tuple] = []
        self.killed: list[tuple] = []

    def probe(self, host):
        return HostProbe(host, 4.0, 8.0, "/tmp/x", 50.0)

    def pids_alive(self, host, pids):
        return {p for p in pids if self.alive.get(p)}

    def tool_processes(self, host, pattern):
        return list(self.procs)

    def kill_pgid(self, host, pgid, sig="TERM"):
        self.killed.append(("pgid", pgid, sig))
        return True

    def run(self, host, cmd, timeout_s=None):
        if isinstance(cmd, str) and cmd.startswith("kill -0 "):
            return (0 if self.alive.get(int(cmd.rsplit("-", 1)[1])) else 1), "", ""
        if isinstance(cmd, str) and cmd.startswith("kill "):
            self.killed.append(("cmd", cmd))
            return 0, "", ""
        return super().run(host, cmd, timeout_s)


class Rec(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.texts: dict[str, tuple[str, str | None]] = {}
        self.boards: list[str] = []
        self.started = 0

    def start(self):
        self.started += 1

    def send(self, kind, run_id, text, buttons=None, cmd=None):
        self.sent.append((kind, run_id))
        self.texts[run_id] = (text, cmd)
        return str(len(self.sent))

    def board(self, text):
        self.boards.append(text)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.project = replace(load_project(DEMO), state_dir=tmp_path / "state", data=tmp_path / "data")
        lim = self.project.limits
        lim.stale_s, lim.dead_s, lim.hung_s, lim.grace_s = 30, 90, 3600, 10
        self.ssh = FakeSsh(self.project.site)
        self.db = Database(tmp_path / "data" / "edr.db")
        self.notifier = Rec()

    def heartbeat(self, label: str, batch: str = "demo", phase: str = "stage:synth", age: float = 5,
                  date: str = OLD, source: str = "gabc1234", **extra) -> dict:
        run_id = rid(label, date, source)
        root = self.tmp / "scratch" / "edr" / "demo" / run_id
        (root / "log").mkdir(parents=True, exist_ok=True)
        (root / "log" / "synth.log").write_text("step 1\n")
        hb = {"schema": 1, "run_id": run_id, "batch": batch, "label": label, "config": "demo", "host": "local",
              "root": str(root), "driver_pid": 4242, "pgids": [4300], "phase": phase, "stage": "synth", "step": 1,
              "step_name": "analyze", "tasks": {}, "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 0,
              "queued": 0}, "started": NOW - 3600, "updated": NOW - age, "elapsed_s": 3600 - age,
              "disk_free_gb": 100.0, "tree_gb": 1.0, "exit": None, "killed_by": None, "last_cmd": "x",
              "last_log": "step 1", "log": str(root / "log" / "synth.log")}
        hb.update(extra)
        (self.project.state_dir / batch).mkdir(parents=True, exist_ok=True)
        (self.project.state_dir / batch / f"{run_id}.json").write_text(json.dumps(hb))
        # launch writes source; the heartbeat has no source.
        self.db.upsert_run({"run_id": run_id, "batch": batch, "label": label, "source": source})
        return hb

    def cycle(self, now: float = NOW, **kw) -> dict[str, str]:
        return watch.cycle(self.project, self.ssh, self.db, [self.notifier], now=now, **kw)

    def events(self) -> list[tuple[str, str]]:
        return [(e["run_id"], e["kind"]) for e in self.db.events(n=500) if e["actor"] == "watch"]

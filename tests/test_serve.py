"""serve.py: the supervisor with fake watcher processes on two demo projects, its check, its unit and its board."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from helpers_cli import edr
from helpers_watch import Rec

from edarunner import census, config, home, serve

NOW = 1_800_000_000.0


class Proc:
    """A watcher process that runs until the test ends it."""

    started: list[Proc] = []

    def __init__(self, argv, cwd, stdin=None) -> None:
        self.argv, self.cwd, self.returncode, self.pid = argv, Path(cwd), None, 5000 + len(Proc.started)
        Proc.started.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def sup(two, monkeypatch):
    Proc.started = []
    monkeypatch.setattr(serve.subprocess, "Popen", Proc)
    monkeypatch.setattr(census, "work", lambda notifiers, now=None, own=None: {"ts": now, "hosts": {}, "runs": []})
    rec = Rec()
    return serve.Supervisor([rec]), rec


def started(name: str) -> list[Proc]:
    return [p for p in Proc.started if p.cwd.name == name]


def test_one_watcher_per_project_and_a_restart_after_the_backoff(sup, two) -> None:
    s, rec = sup
    state = s.cycle(NOW)
    assert [(p.cwd.name, p.argv[-2:]) for p in Proc.started] == [("alpha", ["watch", "--served"]), ("beta", ["watch", "--served"])]
    assert state["projects"]["alpha"]["watcher"] == Proc.started[0].pid
    assert json.loads((home.root() / "serve.json").read_text())["cycle"] == 1
    s.cycle(NOW + 60)
    assert len(Proc.started) == 2  # both still run
    started("alpha")[0].returncode = 1
    s.cycle(NOW + 120)
    assert len(started("alpha")) == 1 and rec.sent == [("watch", "alpha")]
    assert "exited with code 1" in rec.alerts["alpha"].about
    s.cycle(NOW + 179)
    assert len(started("alpha")) == 1
    s.cycle(NOW + 181)
    assert len(started("alpha")) == 2 and len(rec.sent) == 1
    started("alpha")[1].returncode = 1
    s.cycle(NOW + 240)
    s.cycle(NOW + 240 + 119)
    assert len(started("alpha")) == 2  # the second exit waits two minutes, and alerts no more
    s.cycle(NOW + 240 + 121)
    assert len(started("alpha")) == 3 and len(rec.sent) == 1


def test_a_watcher_that_stands_still_is_killed_and_started_again(sup, two) -> None:
    s, rec = sup
    s.cycle(NOW)
    alpha = two["alpha"]
    config.save_json(alpha.state_dir / "watch.json", {"ts": NOW + 30, "cycle": 1, "pid": started("alpha")[0].pid})
    s.cycle(NOW + serve.STUCK_S)
    assert len(started("alpha")) == 1 and rec.sent == []
    config.save_json(two["beta"].state_dir / "watch.json", {"ts": NOW + 30 + serve.STUCK_S, "cycle": 9, "pid": 1})
    s.cycle(NOW + 30 + serve.STUCK_S + 1)
    first = started("alpha")[0]
    assert first.returncode == -15 and len(started("alpha")) == 1 and rec.sent == [("watch", "alpha")]
    assert "wrote no watch.json for 15m, so the supervisor killed it" in rec.alerts["alpha"].about
    assert rec.alerts["alpha"].title == "watcher stuck for"
    s.cycle(NOW + 30 + serve.STUCK_S + 2)
    assert len(started("alpha")) == 2


def test_a_project_that_does_not_load_gets_no_watcher_and_one_alert(sup, two) -> None:
    s, rec = sup
    edr_toml = two["beta"].root / "edr.toml"
    text = edr_toml.read_text()
    edr_toml.write_text(text + "\nbogus = 1\n")
    s.cycle(NOW)
    s.cycle(NOW + 60)
    assert [p.cwd.name for p in Proc.started] == ["alpha"] and rec.sent == [("config", "beta")]
    assert rec.alerts["beta"].about.startswith("No watcher runs for the project until the file loads")
    edr_toml.write_text(text)
    s.cycle(NOW + 120)
    assert [p.cwd.name for p in Proc.started] == ["alpha", "beta"]


def test_a_project_that_another_watcher_watches_or_that_left_the_registry(sup, two) -> None:
    s, rec = sup
    fd = home.lock(two["beta"].state_dir / "watch.lock")
    try:
        state = s.cycle(NOW)
    finally:
        os.close(fd)
    assert [p.cwd.name for p in Proc.started] == ["alpha"] and state["projects"]["beta"]["note"] == f"watched by pid {os.getpid()}"
    home.unregister("alpha", two["alpha"].root)
    s.cycle(NOW + 60)
    assert started("alpha")[0].returncode == -15 and "alpha" not in s.children and len(started("beta")) == 1
    s.stop()
    assert started("beta")[0].returncode == -15


def test_check_reads_serve_json_only(two, capsys) -> None:
    rec = Rec()
    (two["alpha"].root / "edr.toml").write_text("broken")
    assert serve.check([rec], NOW) == 1 and rec.sent == [("watch", "serve")]
    assert rec.alerts["serve"].about.startswith("The supervisor has never finished a cycle.")
    config.save_json(home.root() / "serve.json", {"ts": NOW - 60, "cycle": 3, "pid": 77})
    assert serve.check([rec], NOW) == 0
    assert serve.check([rec], NOW + 3 * serve.CYCLE_S) == 1 and "for 4m" in rec.alerts["serve"].about
    assert "serve.json is 240 s old (pid 77)" in capsys.readouterr().out


def test_systemd_hears_ready_and_the_watchdog(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        serve._notify("READY=1")
        assert s.recv(64) == b"READY=1"
    monkeypatch.delenv("NOTIFY_SOCKET")
    serve._notify("WATCHDOG=1")  # no socket, no error


def test_a_second_supervisor_exits_2(two, capsys) -> None:
    fd = home.lock(home.root() / "serve.lock")
    try:
        assert serve.run([], once=True) == 2
    finally:
        os.close(fd)
    assert f"pid {os.getpid()} serves already" in capsys.readouterr().out


def test_the_unit_runs_a_pinned_copy_of_a_checkout(two, capsys, monkeypatch) -> None:
    monkeypatch.chdir(two["alpha"].root)
    code, out, err = edr(capsys, "serve", "--unit", "--dry-run")
    repo = Path(serve.__file__).resolve().parents[2]
    assert code == 0 and err.startswith(f"dry: uv tool install --force git+file://{repo}@")
    assert "Type=notify" in out and "WatchdogSec=600" in out and "Restart=always" in out
    exe = next(ln for ln in out.splitlines() if ln.startswith("ExecStart=")).split("=", 1)[1]
    assert exe.endswith("/edr serve") and not exe.startswith(str(repo)) and f"of {repo}" in out
    monkeypatch.setattr(serve.importlib.metadata, "distribution", lambda name: type("D", (), {"read_text": lambda s, f: None})())
    monkeypatch.setattr(serve.shutil, "which", lambda name: "/checkout/.venv/bin/edr")
    monkeypatch.setattr(serve.sys, "argv", ["/home/me/.local/bin/edr", "serve", "--unit"])
    cmd, exe, what = serve.pinned()  # the installed copy that runs, not the checkout first on PATH
    assert cmd == [] and exe == "/home/me/.local/bin/edr" and what.startswith("edarunner ")
    monkeypatch.setattr(serve.sys, "argv", ["/x/cli.py"])
    assert serve.pinned()[1] == "/checkout/.venv/bin/edr"


def test_dry_run_lists_what_the_supervisor_would_do(two, capsys) -> None:
    config.save_json(two["beta"].state_dir / "watch.json", {"ts": __import__("time").time(), "cycle": 1, "pid": 42})
    code, out, _ = edr(capsys, "serve", "--dry-run")
    assert code == 0 and "start edr watch --served" in out and "none, pid 42 watches it" in out
    assert not (home.root() / "serve.json").exists()


def test_the_global_board_names_every_project_and_your_hosts(two) -> None:
    alpha, beta = two["alpha"], two["beta"]
    row = {"run_id": "r1", "label": "a", "batch": "demo", "phase": "stage:synth", "state": "running", "stage": "synth",
           "step": 2, "started": NOW - 3600, "updated": NOW - 5}
    config.save_json(alpha.data / "board" / "board.json", {"runs": [row, {**row, "run_id": "r2", "label": "d", "state": "dead"}]})
    config.save_json(beta.data / "board" / "board.json", {"runs": [{**row, "phase": "done", "updated": NOW - 60}]})
    probe = {"host": "local", "free_cores": 2.0, "free_ram_gb": 4.0, "mount": "/s", "free_gb": 50.0, "cores": 4,
             "total_ram_gb": 8.0, "total_gb": 100.0, "gpus": 0, "gpus_idle": 0}
    text = serve.global_board(two, {"hosts": {"local": probe}, "runs": [
        {"project": "alpha", "host": "local", "cpu_pct": 200.0, "tree_gb": 7.0}]}, NOW)
    lines = text.splitlines()
    assert lines[:4] == ["<b>alpha</b>", "#1 🔴 <code>d@demo</code> dead, synth 2/4 elaborate, 1h",
                         "#2 🟢 <code>a@demo</code> synth 2/4 elaborate, 1h", "<b>beta</b> <i>nothing live, 1 ended in 24 h</i>"]
    assert home.Store().get_store("last_board") == [["alpha", "r2"], ["alpha", "r1"]]
    assert lines[5:7] == ["<b>Machines</b>", "<b>local</b> free 2/4 cores, 4/8 GB RAM, 50/100 GB scratch; yours: alpha 1, "
                          "2 cores, 7 GB scratch"]
    assert lines[-1] == "<i>2 live: 1 dead, 1 running. /status &lt;project&gt; shows one project.</i>"

"""cli.py: main, Ctx and check on a copy of examples/local-demo; the commands are in test_cli.py."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from pathlib import Path

from helpers_cli import edr

from edarunner import checkout, cli, home, launch
from edarunner.hosts import HostProbe, Ssh


def test_main_reports_an_unhandled_exception(capsys, caplog, monkeypatch) -> None:
    def boom(c, a):
        raise KeyError("runs: unknown columns ['x']")

    monkeypatch.setattr(cli, "cmd_hosts", boom)
    code, out, err = edr(capsys, "--json", "hosts")
    assert code == 1 and json.loads(out)["code"] == 1
    assert "edr: \"runs: unknown columns ['x']\"" in err
    assert caplog.records[-1].exc_info is not None


def test_read_verbs_create_no_database(demo: Path, capsys) -> None:
    assert edr(capsys, "status")[0] == 0
    assert edr(capsys, "events")[0] == 2
    assert edr(capsys, "check")[0] == 0
    assert not (demo / "data").exists()


def test_project_is_found_from_a_subdirectory(demo: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(demo / "jobs")
    code, out, _ = edr(capsys, "check")
    assert code == 0 and out.startswith("ok:")
    monkeypatch.chdir(demo.parent)
    code, _, err = edr(capsys, "status")
    assert code == 1 and f"no edr.toml in {demo.parent} or above; run edr init" in err


def test_check_names_the_missing_head_node_tools(demo: Path, capsys, monkeypatch, tmp_path: Path) -> None:
    thin = tmp_path / "bin"
    thin.mkdir()
    (thin / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(thin))
    code, out, _ = edr(capsys, "--json", "check")
    problems = json.loads(out)["data"]["problems"]
    assert code == 1 and "local: rsync not on PATH" in problems and "local: python3 not on PATH" in problems
    assert "local: git not on PATH" not in problems and "local: ssh not on PATH" not in problems


def test_check_probes_each_host_once_and_resolves_the_source(demo: Path, capsys, monkeypatch) -> None:
    probed: list[str] = []
    planned: list[tuple[str, list[str]]] = []
    real_plan = launch.plan

    def probe(self, host):
        probed.append(host)
        return HostProbe(host, 4.0, 8.0, "/tmp/x", 50.0)

    def plan(project, batch, ssh, db, *a, **kw):
        planned.append((batch.source, sorted(kw.get("probes") or {})))
        return real_plan(project, batch, ssh, db, *a, **kw)

    monkeypatch.setattr(Ssh, "probe", probe)
    monkeypatch.setattr(launch, "plan", plan)
    jobs = demo / "jobs"
    shutil.copy(jobs / "demo.toml", jobs / "second.toml")
    assert edr(capsys, "check")[0] == 0
    # Two batches, one probe; the ref stays as written while nothing is checked out.
    assert probed == ["local"] and planned == [("HEAD", ["local"])] * 2
    tree = demo / "wt" / "deadbee"
    tree.mkdir(parents=True)
    (tree / "source.json").write_text('{"source": "deadbee"}')
    monkeypatch.setattr(checkout, "find", lambda project, source: tree)
    assert edr(capsys, "check")[0] == 0 and planned[2:] == [("deadbee", ["local"])] * 2


def _head(demo: Path, tmp_path: Path, monkeypatch, ssh_exit: str = 'eval "$last"') -> Path:
    """A site with head = "head", this host host1, and an ssh on PATH that logs its arguments and runs the remote
    command here with an edr that prints where it runs; returns the log."""
    site = demo / "site.toml"
    site.write_text('head = "head"\n' + site.read_text())
    bin_, log = tmp_path / "bin", tmp_path / "ssh.log"
    bin_.mkdir()
    (bin_ / "ssh").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\nfor a; do last=$a; done\n{ssh_exit}\n')
    (bin_ / "edr").write_text('#!/bin/sh\necho "pwd=$(pwd) argv=$* fwd=$EDR_FORWARDED batch=$EDR_BATCH"\nexit 3\n')
    for f in ("ssh", "edr"):
        (bin_ / f).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setattr(sys, "argv", [str(bin_ / "edr")])
    monkeypatch.setattr(socket, "gethostname", lambda: "host1.example.org")
    return log


def test_a_host_other_than_the_head_runs_the_command_on_the_head_in_the_project_directory(demo: Path, capfd,
                                                                                         monkeypatch, tmp_path: Path) -> None:
    log = _head(demo, tmp_path, monkeypatch)
    monkeypatch.chdir(demo / "jobs")
    monkeypatch.setenv("EDR_BATCH", "b 1")
    assert cli.main(["--json", "status"]) == 3
    out = capfd.readouterr().out
    assert out == f"pwd={demo / 'jobs'} argv=--json status fwd=1 batch=b 1\n"
    assert "head" in log.read_text().splitlines() and "BatchMode=yes" in log.read_text()
    home.register("demo", demo)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-P", "demo", "export", "--source", "abc1234", "--out", "exports/x"]) == 3
    assert capfd.readouterr().out.startswith(f"pwd={os.path.realpath(demo)} argv=-P demo export --source abc1234 --out exports/x ")


def test_the_head_and_init_run_here_and_a_failed_ssh_names_the_head(demo: Path, capsys, monkeypatch, tmp_path: Path) -> None:
    log = _head(demo, tmp_path, monkeypatch, ssh_exit="exit 255")
    code, _, err = edr(capsys, "status")
    assert code == 255 and "ssh head failed; edr runs this command on the head node head" in err
    monkeypatch.setattr(socket, "gethostname", lambda: "head")
    log.unlink()
    assert edr(capsys, "status")[0] == 0 and not log.exists()
    monkeypatch.setattr(socket, "gethostname", lambda: "host1")
    assert edr(capsys, "init", "--site", str(demo), "--dry-run")[0] in (0, 1) and not log.exists()


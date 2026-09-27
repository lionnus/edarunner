"""cli.py: main, Ctx and check on a copy of examples/local-demo; the verbs are in test_cli.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from edarunner import cli, launch, stagectl
from edarunner.hosts import HostProbe, Ssh
from test_cli import demo, edr  # noqa: F401 - the fixture and the runner of test_cli


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
    # Two batches, one probe; the ref stays as written while nothing is staged.
    assert probed == ["local"] and planned == [("HEAD", ["local"])] * 2
    staged = demo / "wt" / "deadbee"
    staged.mkdir(parents=True)
    (staged / "source.json").write_text('{"src": "deadbee"}')
    monkeypatch.setattr(stagectl, "find", lambda project, src: staged)
    assert edr(capsys, "check")[0] == 0 and planned[2:] == [("deadbee", ["local"])] * 2

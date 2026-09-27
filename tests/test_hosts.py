"""hosts.py: the ssh wrapper on `local`, the probe, pid_alive, kill_pgid, placement."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from edarunner.guards import Refuse
from edarunner.hosts import HostError, HostProbe, Ssh, place
from edarunner.model import (
    Host, Job, Limits, Needs, Placement, Project, Safety, Site, Source, Stage, Sync,
)

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"


def demo_site(scratch: list[str]) -> Site:
    t = tomllib.loads((DEMO / "site.toml").read_text())
    return Site(
        path=DEMO / "site.toml",
        scratch=scratch,
        env=t["env"],
        ssh_options=t["ssh"]["options"],
        ssh_timeout_s=t["ssh"]["timeout_s"],
        tool_procs=t["tool_procs"],
        hosts={n: Host(n, h["cores"], h["ram_gb"], h.get("scratch")) for n, h in t["hosts"].items()},
    )


def demo_project(site: Site) -> Project:
    t = tomllib.loads((DEMO / "edr.toml").read_text())
    stages = {
        n: Stage(n, needs=Needs(s["needs"]["cores"], s["needs"]["disk_gb"], s["needs"].get("licence")))
        for n, s in t["stages"].items()
    }
    return Project(
        root=DEMO, project=t["project"], site=site, state=Path("/nonexistent"), data=Path("data"),
        run_prefix=t["run_prefix"],
        source=Source(Path("repo"), Path("wt"), "HEAD", [], t["source"]["run_id"], ""),
        sync=Sync(exclude=[".git"]), safety=Safety("/edr/", 3), limits=Limits(),
        placement=Placement(**t["placement"]), stages=stages, metrics={},
    )


@pytest.fixture
def ssh(tmp_path: Path) -> Ssh:
    return Ssh(demo_site([str(tmp_path)]))


def fake_run(monkeypatch, table: dict[str, tuple[int, str, str]]) -> list[tuple[str, str]]:
    """Ssh.run that answers from `table` by the first key found in the command."""
    calls: list[tuple[str, str]] = []

    def run(self, host, cmd, timeout_s=None):
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        calls.append((host, text))
        for key, reply in table.items():
            if key in text:
                return reply
        raise AssertionError(f"no canned reply for {text!r}")

    monkeypatch.setattr(Ssh, "run", run)
    return calls


# run


def test_run_local_string_and_argv(ssh: Ssh) -> None:
    assert ssh.run("local", "echo hi; echo err >&2; exit 3") == (3, "hi\n", "err\n")
    assert ssh.run("local", ["echo", "a b"]) == (0, "a b\n", "")


def test_run_local_timeout_is_255(ssh: Ssh) -> None:
    rc, out, err = ssh.run("local", "echo partial; sleep 5", timeout_s=0.3)
    assert rc == 255 and out == "partial\n" and "timeout" in err


def test_run_remote_builds_ssh_argv(ssh: Ssh, monkeypatch) -> None:
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    ssh.run("hostx", "nproc")
    ssh.run("hostx", ["ls", "a b"])
    opts = ssh.site.ssh_options
    # sh -c keeps a tcsh login shell out of the command.
    assert seen == [["ssh", *opts, "hostx", "sh -c nproc"], ["ssh", *opts, "hostx", "sh -c " + shlex.quote("ls 'a b'")]]


# probe


def test_probe_local(ssh: Ssh, tmp_path: Path) -> None:
    tool = subprocess.Popen(["sleep", "30"])
    driver = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "edr_driver.py"])
    try:
        p = ssh.probe("local")
    finally:
        tool.kill(), driver.kill(), tool.wait(), driver.wait()
    assert p.host == "local"
    assert 0 < p.free_cores <= os.cpu_count()
    assert p.free_ram_gb > 0
    assert p.mount == str(tmp_path) and p.free_gb > 0
    assert p.our_tool_procs >= 1 and p.our_runs >= 1


def test_probe_picks_largest_writable_scratch(ssh: Ssh, tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    if os.access(ro, os.W_OK):
        pytest.skip("root ignores directory modes")
    ssh.site.scratch = [str(ro), "/nonexistent", str(tmp_path)]
    assert ssh.probe("local").mount == str(tmp_path)


def test_probe_parses_canned_output(ssh: Ssh, monkeypatch) -> None:
    out = "\n".join([
        "me", "8", "2.5", "38750732", "@@",
        "/scratch 104857600", "/scratch2 209715200", "@@",
        "me sleep", "other sleep", "me bash", "me sleep", "@@",
        "me python3 /x/edr_driver.py a.json", "other python3 /y/edr_driver.py b.json", "",
    ])
    fake_run(monkeypatch, {"nproc": (0, out, "")})
    assert ssh.probe("h") == HostProbe("h", 5.5, 37.0, "/scratch2", 200.0, 2, 1, 1)


def test_probe_failure_raises(ssh: Ssh, monkeypatch) -> None:
    fake_run(monkeypatch, {"nproc": (255, "", "ssh: connect timed out")})
    with pytest.raises(HostError, match="rc 255"):
        ssh.probe("h")
    fake_run(monkeypatch, {"nproc": (0, "garbage\n", "")})
    with pytest.raises(HostError, match="unreadable"):
        ssh.probe("h")


def test_scratch_dirs_prefers_host_list(ssh: Ssh) -> None:
    ssh.site.hosts["h"] = Host("h", 4, 8, ["/s1"])
    assert ssh.scratch_dirs("h") == ["/s1"]
    assert ssh.scratch_dirs("local") == ssh.site.scratch


# pid_alive, kill_pgid, tool_processes


def test_pid_alive_local(ssh: Ssh) -> None:
    assert ssh.pid_alive("local", os.getpid())
    assert not ssh.pid_alive("local", 4194303)


def test_pid_alive_canned(ssh: Ssh, monkeypatch) -> None:
    fake_run(monkeypatch, {"ps -p 42": (0, "42\n", ""), "ps -p 43": (1, "", "")})
    assert ssh.pid_alive("h", 42) and not ssh.pid_alive("h", 43)
    fake_run(monkeypatch, {"ps -p": (255, "", "lost")})
    with pytest.raises(HostError):
        ssh.pid_alive("h", 42)


def test_kill_pgid_local(ssh: Ssh) -> None:
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    assert ssh.kill_pgid("local", p.pid)
    assert p.wait(timeout=5) == -15
    assert not ssh.kill_pgid("local", p.pid)


@pytest.mark.parametrize("pgid", [0, 1, -5, True, "7"])
def test_kill_pgid_refuses_bad_pgid(ssh: Ssh, pgid) -> None:
    with pytest.raises(Refuse):
        ssh.kill_pgid("local", pgid)


def test_kill_pgid_refuses_bad_signal(ssh: Ssh) -> None:
    with pytest.raises(Refuse):
        ssh.kill_pgid("local", 4242, "TERM; rm -rf /")


def test_tool_processes_local(ssh: Ssh) -> None:
    p = subprocess.Popen(["sleep", "31"])
    try:
        rows = ssh.tool_processes("local", ssh.site.tool_procs)
    finally:
        p.kill(), p.wait()
    mine = [r for r in rows if r[0] == p.pid]
    assert mine and mine[0][3] == "sleep" and mine[0][4] == "sleep 31"
    assert isinstance(mine[0][1], int) and isinstance(mine[0][2], float)


def test_check_local_finds_every_tool_here(ssh: Ssh) -> None:
    assert ssh.check_local() == []


# place


def probe(host: str, cores: float = 8, ram: float = 32, disk: float = 100) -> HostProbe:
    return HostProbe(host, cores, ram, "/scratch", disk)


def job(label: str, host: str = "auto") -> Job:
    return Job(label=label, config="demo", host=host, stages=["synth"])


def test_place_prefers_then_free_cores(ssh: Ssh) -> None:
    proj = demo_project(ssh.site)
    probes = {"a": probe("a", cores=8), "b": probe("b", cores=32), "c": probe("c", cores=16)}
    assert place(proj, [job("j")], probes, {}) == {"j": "b"}
    proj.placement = replace(proj.placement, prefer=["c"])
    assert place(proj, [job("j")], probes, {}) == {"j": "c"}


def test_place_avoid_max_per_host_and_queue(ssh: Ssh) -> None:
    proj = demo_project(ssh.site)
    proj.placement = replace(proj.placement, avoid=["a"], max_per_host=1)
    probes = {"a": probe("a", cores=64), "b": probe("b", cores=4)}
    got = place(proj, [job("j1"), job("j2")], probes, {"b": 0})
    assert got == {"j1": "b", "j2": None}
    assert place(proj, [job("j1")], probes, {"b": 1}) == {"j1": None}


def test_place_fits_nowhere(ssh: Ssh) -> None:
    proj = demo_project(ssh.site)
    assert place(proj, [job("j")], {}, {}) == {"j": None}
    low = {"a": probe("a", cores=0.5), "b": probe("b", ram=0.5), "c": probe("c", disk=0.1)}
    assert place(proj, [job("j")], low, {}) == {"j": None}


def test_place_fixed_host_and_needs_subtraction(ssh: Ssh) -> None:
    proj = demo_project(ssh.site)
    proj.stages["synth"].needs = Needs(cores=6, disk_gb=60)
    proj.placement = replace(proj.placement, max_per_host=4)
    probes = {"a": probe("a", cores=8, disk=100)}
    got = place(proj, [job("f", host="zz"), job("j1"), job("j2")], probes, {})
    assert got == {"f": "zz", "j1": "a", "j2": None}

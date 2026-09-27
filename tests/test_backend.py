"""backend.py: the handle, the ssh backend on canned `ps` output, and the local backend on real processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from edarunner import config
from edarunner.backend import Handle, Live, LocalBackend, Request, SshBackend, make_backend, run_handle
from edarunner.guards import Refuse
from edarunner.hosts import Ssh

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"


class CannedSsh(Ssh):
    """Answers `ps -p` from a table of live pids per host; a host outside the table does not answer."""

    def __init__(self, site, live: dict[str, set[int]]) -> None:
        super().__init__(site)
        self.live, self.cmds = live, []

    def run(self, host, cmd, timeout_s=None):
        self.cmds.append((host, cmd))
        if host not in self.live:
            return 255, "", "ssh: connect timed out"
        if cmd.startswith("ps -p "):
            pids = [int(p) for p in cmd.split()[2].split(",")]
            found = [p for p in pids if p in self.live[host]]
            return (0 if found else 1), "".join(f"{p:>7}\n" for p in found), ""
        return 0, "", ""


@pytest.fixture
def site():
    return config.load_project(DEMO).site


def test_handle_text_and_pid() -> None:
    h = Handle("ssh", "hostA:4242", "hostA")
    assert str(h) == "ssh:hostA:4242" and Handle.parse(str(h)) == h and h.pid == 4242
    assert Handle("ssh", "hostA:", "hostA").pid is None
    assert Handle.parse("condor:12.0") == Handle("condor", "12.0", None)
    assert run_handle("ssh", {"host": "a"}, {"driver_pid": 7}) == Handle("ssh", "a:7", "a")
    assert run_handle("ssh", {"host": "a", "handle": "ssh:b:9"}, {}) == Handle("ssh", "b:9", "b")
    assert run_handle("ssh", {"host": "a"}, {}) is None


def test_make_backend_follows_the_site(site) -> None:
    assert make_backend(site).name == "ssh"
    assert isinstance(make_backend(replace(site, scheduler_backend="local")), LocalBackend)


def test_ssh_alive_asks_each_host_once(site) -> None:
    ssh = CannedSsh(site, {"a": {11, 13}, "b": {21}})
    hs = [Handle("ssh", f"{host}:{pid}", host) for host, pid in (("a", 11), ("a", 12), ("a", 13), ("b", 21), ("c", 31))]
    nopid = Handle("ssh", "a:", "a")
    out = SshBackend(ssh).alive([*hs, nopid])
    assert [out[h][0] for h in hs] == [Live.RUNNING, Live.GONE, Live.RUNNING, Live.RUNNING, Live.UNKNOWN]
    assert out[nopid] == (Live.GONE, "no driver pid") and "c: rc 255" in out[hs[4]][1]
    assert sorted(ssh.cmds) == [("a", "ps -p 11,12,13 -o pid="), ("b", "ps -p 21 -o pid="), ("c", "ps -p 31 -o pid=")]


def test_ssh_stop_signals_the_driver_and_the_groups(site) -> None:
    ssh = CannedSsh(site, {"a": set()})
    SshBackend(ssh).stop(Handle("ssh", "a:4242", "a"), False, [4300])
    SshBackend(ssh).stop(Handle("ssh", "a:", "a"), True, [4300])
    assert ssh.cmds == [("a", "kill -TERM 4242"), ("a", "kill -TERM -- -4300"), ("a", "kill -KILL -- -4300")]
    with pytest.raises(Refuse):
        SshBackend(ssh).stop(Handle("ssh", "a:1", "a"), False)


def test_ssh_free_keeps_the_error_of_a_host(site) -> None:
    free = SshBackend(CannedSsh(site, {})).free(["far"])
    assert isinstance(free["far"], str) and "rc 255" in free["far"]


def test_local_backend_submit_alive_stop(site, tmp_path: Path) -> None:
    driver = tmp_path / "driver.py"
    driver.write_text("import sys, time\nprint(sys.argv[1], flush=True)\ntime.sleep(60)\n")
    req = Request("r", tmp_path / "r.spec.json", driver, tmp_path / "r.driver.log", env={"EDR_X": "1"})
    be = LocalBackend(Ssh(site))
    h = be.submit(req)
    assert h.backend == "local" and h.host == "local" and h.pid and os.getpgid(h.pid) == h.pid
    assert be.alive([h])[h][0] is Live.RUNNING
    log, end = tmp_path / "r.driver.log", time.time() + 10
    while not log.read_text() and time.time() < end:
        time.sleep(0.05)
    assert log.read_text().strip() == str(req.spec)
    group = subprocess.Popen(["sleep", "60"], start_new_session=True)
    be.stop(h, False, [group.pid])
    assert group.wait(timeout=10) == -signal.SIGTERM
    end = time.time() + 10
    while be.alive([h])[h][0] is not Live.GONE and time.time() < end:
        time.sleep(0.1)
    assert be.alive([h])[h][0] is Live.GONE
    be.stop(h, True, [group.pid])  # a gone driver and a gone group are no error
    with pytest.raises(Refuse):
        be.stop(Handle("local", "local:", "local"), False, [1])


def test_local_backend_sees_a_process_it_did_not_start(site) -> None:
    be = LocalBackend(Ssh(site))
    me = Handle("local", f"local:{os.getpid()}", "local")
    assert be.alive([me])[me][0] is Live.RUNNING
    p = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    gone = Handle("local", f"local:{p.stdout.strip()}", "local")
    assert be.alive([gone])[gone][0] is Live.GONE

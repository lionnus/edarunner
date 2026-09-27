"""The backends that start, watch and stop a driver: `ssh` on the site hosts, `local` on the head node,
and the batch schedulers of `schedulers.py`.

A backend knows how a driver starts and whether it still runs. Everything else of a run, the
spec, the heartbeat, the stop and keep files, lives in the state directory and is the same for
every backend. `make_backend` picks the class by `[scheduler] backend` of `site.toml`.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .guards import Refuse
from .hosts import HostError, HostProbe, Ssh
from .model import SCHEDULERS, Site


class Live(Enum):
    """What a backend knows of one driver."""

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    HELD = "held"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Request:
    """What one run asks of a backend: the driver, its spec and its log, and a host when the run has one.

    The resources are for a scheduler, which reserves them for the whole job: the most cores, RAM and
    disk any stage asks, the wall time when every stage has a budget, and the scheduler licences with
    the most seats any stage needs."""

    run_id: str
    spec: Path
    driver: Path
    log: Path
    host: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    project: str = ""
    cores: int = 1
    ram_gb: float = 0.0
    disk_gb: float = 0.0
    hours: float | None = None
    licences: dict[str, int] = field(default_factory=dict)
    queue: str = ""
    options: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Handle:
    """A started driver as its backend names it; `id` is `<host>:<pid>` for `ssh`, `local` and `track`.

    `track` names a driver that `edr track` runs in the foreground, outside every backend."""

    backend: str
    id: str
    host: str | None = None

    def __str__(self) -> str:
        return f"{self.backend}:{self.id}"

    @classmethod
    def parse(cls, text: str) -> Handle:
        """The handle that `str(handle)` wrote into `runs.handle`."""
        backend, _, ident = text.partition(":")
        host = ident.rpartition(":")[0] if backend in ("ssh", "local", "track") else ""
        return cls(backend, ident, host or None)

    @property
    def pid(self) -> int | None:
        """The driver pid of a `<host>:<pid>` id, else None."""
        tail = self.id.rpartition(":")[2]
        return int(tail) if tail.isdigit() else None


class Backend(Protocol):
    """Start, watch and stop drivers; `free` probes the hosts, or gives None when a scheduler places."""

    name: str

    def submit(self, req: Request) -> Handle: ...

    def alive(self, handles: Iterable[Handle]) -> dict[Handle, tuple[Live, str]]: ...

    def stop(self, handle: Handle, hard: bool, pgids: Sequence[int] = ()) -> None: ...

    def free(self, hosts: Iterable[str]) -> dict[str, HostProbe | str] | None: ...

    def file_host(self, run: dict[str, Any]) -> str: ...


def make_backend(site: Site, ssh: Ssh | None = None) -> Backend:
    """The backend of `[scheduler] backend`; `ssh` is the wrapper it uses, a new one when None."""
    ssh = ssh or Ssh(site)
    name = site.scheduler.backend
    if name in SCHEDULERS:
        from .schedulers import BY_NAME
        return BY_NAME[name](site.scheduler)
    return LocalBackend(ssh) if name == "local" else SshBackend(ssh)


def run_handle(name: str, run: dict[str, Any], hb: dict[str, Any]) -> Handle | None:
    """The handle of a run: the driver pid of the heartbeat, else the handle the backend gave at submit.

    Under a scheduler it is the job id: the one of the last submit, else the one the driver wrote."""
    if name in SCHEDULERS:
        if str(run.get("handle") or "").startswith(f"{name}:"):
            return Handle.parse(str(run["handle"]))
        return Handle(name, str(hb["sched_id"])) if hb.get("sched_id") else None
    host, pid = hb.get("host") or run.get("host"), hb.get("driver_pid")
    if host and pid:
        return Handle(name, f"{host}:{pid}", str(host))
    return Handle.parse(str(run["handle"])) if run.get("handle") else None


def gone(backend: Backend, handle: Handle) -> bool:
    """True when the driver of `handle` no longer runs; HostError when the backend could not tell."""
    state, why = backend.alive([handle])[handle]
    if state is Live.UNKNOWN:
        raise HostError(why)
    return state is Live.GONE


def check_pid(pid: object) -> None:
    """Refuse a pid that is not an int above 1."""
    # pid 0 or a negative number would signal the shell's group or every process.
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1):
        raise Refuse(f"'{pid}' is not a pid")


def _start_local(req: Request) -> int:
    full = dict(os.environ)
    full.update({k: os.path.expandvars(v) for k, v in req.env.items()})
    with open(req.log, "ab") as log:
        p = subprocess.Popen(["python3", str(req.driver), str(req.spec)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, env=full, start_new_session=True)
    return p.pid


class SshBackend:
    """The driver runs in its own session on a site host, started over ssh; the host `local` runs without ssh."""

    name = "ssh"

    def __init__(self, ssh: Ssh) -> None:
        self.ssh = ssh

    def submit(self, req: Request) -> Handle:
        host = req.host
        if not host:
            raise HostError(f"{req.run_id}: the ssh backend needs a host")
        if host == "local":
            return Handle(self.name, f"local:{_start_local(req)}", "local")
        # The interpreter is the host's own python3 from the login PATH: a tool PATH once put a
        # Python 3.4 first, and the driver died at its first subprocess.run. The site env reaches
        # the driver's children through the spec, so nothing is exported here.
        cmd = (f"py=$(command -v python3); setsid nohup \"$py\" {shlex.quote(str(req.driver))} "
               f"{shlex.quote(str(req.spec))} > {shlex.quote(str(req.log))} 2>&1 < /dev/null & echo $!")
        rc, out, err = self.ssh.run(host, cmd)
        if rc != 0:
            raise HostError(f"{host}: rc {rc}: {err.strip()}")
        last = out.split()[-1] if out.split() else ""
        return Handle(self.name, f"{host}:{last if last.isdigit() else ''}", host)

    def alive(self, handles: Iterable[Handle]) -> dict[Handle, tuple[Live, str]]:
        """One `ps` per host for every handle on it."""
        out: dict[Handle, tuple[Live, str]] = {}
        per_host: dict[str, list[Handle]] = {}
        for h in handles:
            if h.pid is None or not h.host:
                out[h] = (Live.GONE, "no driver pid")
            else:
                per_host.setdefault(h.host, []).append(h)
        for host, hs in per_host.items():
            try:
                found = self.ssh.pids_alive(host, [int(h.pid or 0) for h in hs])
            except HostError as e:
                out.update({h: (Live.UNKNOWN, str(e)) for h in hs})
                continue
            out.update({h: (Live.RUNNING, "") if h.pid in found else (Live.GONE, f"driver {h.pid} gone on {host}")
                        for h in hs})
        return out

    def stop(self, handle: Handle, hard: bool, pgids: Sequence[int] = ()) -> None:
        """TERM, or KILL with `hard`, to the driver and to every process group."""
        sig, host = "KILL" if hard else "TERM", str(handle.host)
        check_pid(handle.pid)
        if handle.pid:
            self.ssh.run(host, f"kill -{sig} {handle.pid}")
        for pgid in pgids:
            self.ssh.kill_pgid(host, pgid, sig)

    def free(self, hosts: Iterable[str]) -> dict[str, HostProbe | str]:
        """The probe of each host, or the error text of a host that did not answer."""
        out: dict[str, HostProbe | str] = {}
        for h in hosts:
            try:
                out[h] = self.ssh.probe(h)
            except HostError as e:
                out[h] = str(e)
        return out

    def file_host(self, run: dict[str, Any]) -> str:
        """The run's host; `local` for a run of `edr track`, whose tree the head node reads in place."""
        return "local" if str(run.get("handle") or "").startswith("track:") else str(run.get("host") or "")


class LocalBackend(SshBackend):
    """The driver runs on the head node itself: a process in its own session, watched with signal 0."""

    name = "local"

    def submit(self, req: Request) -> Handle:
        return Handle(self.name, f"local:{_start_local(req)}", "local")

    def alive(self, handles: Iterable[Handle]) -> dict[Handle, tuple[Live, str]]:
        out: dict[Handle, tuple[Live, str]] = {}
        for h in handles:
            pid = h.pid
            if pid is None:
                out[h] = (Live.GONE, "no driver pid")
                continue
            try:
                # A driver this process started stays a zombie until it is reaped.
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    out[h] = (Live.GONE, f"driver {pid} ended")
                    continue
            except ChildProcessError:
                pass
            try:
                os.kill(pid, 0)
                out[h] = (Live.RUNNING, "")
            except ProcessLookupError:
                out[h] = (Live.GONE, f"driver {pid} gone")
            except PermissionError:
                out[h] = (Live.RUNNING, "")
        return out

    def stop(self, handle: Handle, hard: bool, pgids: Sequence[int] = ()) -> None:
        sig = signal.SIGKILL if hard else signal.SIGTERM
        check_pid(handle.pid)
        for pgid in pgids:
            if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
                raise Refuse(f"'{pgid}' is not a process group id")
        for kill, target in ([(os.kill, handle.pid)] if handle.pid else []) + [(os.killpg, g) for g in pgids]:
            try:
                kill(target, sig)
            except ProcessLookupError:
                pass

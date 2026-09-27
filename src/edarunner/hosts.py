"""The ssh wrapper, the host probe and the placement. See docs/design.md 3.1, 3.2, 6.

Every remote command is one short shell string, run by `sh -c` so the
login shell of the host (tcsh on many farms) never parses it. The host
`local` runs on the head node without ssh.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace

from .guards import Refuse
from .model import Job, Needs, Placement, Project, Site

TIMEOUT_RC = 255
# What the head node runs itself: the controller calls the first four, the `local` host the rest.
HEAD_TOOLS = ("ssh", "rsync", "git", "python3", "nproc", "df", "ps", "awk", "stat", "readlink")
_SEP = "@@"
_SIG_RE = re.compile(r"^[A-Z0-9]+$")


class HostError(OSError):
    """A host did not answer, or answered with text the probe cannot read."""


@dataclass
class HostProbe:
    host: str
    free_cores: float
    free_ram_gb: float
    mount: str
    free_gb: float
    our_tool_procs: int = 0
    other_tool_procs: int = 0
    our_runs: int = 0


# Placement


def _first_needs(project: Project, job: Job) -> Needs:
    names = job.stages or list(project.stages)
    return project.stages[names[0]].needs if names else Needs()


def _fits(pl: Placement, p: HostProbe, running: int, needs: Needs) -> bool:
    return (
        p.host not in pl.avoid
        and running < pl.max_per_host
        and p.free_cores >= max(pl.min_free_cores, needs.cores)
        and p.free_ram_gb >= pl.min_free_ram_gb
        and p.free_gb > needs.disk_gb
    )


def place(
    project: Project,
    jobs: list[Job],
    probes: dict[str, HostProbe],
    running_per_host: dict[str, int],
) -> dict[str, str | None]:
    """Give each job a host by the design 3.1 rules; None means queue."""
    pl = project.placement
    free = {h: replace(p) for h, p in probes.items()}
    running = dict(running_per_host)
    prefer = {h: i for i, h in enumerate(pl.prefer)}
    out: dict[str, str | None] = {}

    def take(host: str, needs: Needs) -> None:
        running[host] = running.get(host, 0) + 1
        if host in free:
            free[host].free_cores -= needs.cores
            free[host].free_gb -= needs.disk_gb

    for job in jobs:
        needs = _first_needs(project, job)
        if job.host != "auto":
            out[job.label] = job.host
            take(job.host, needs)
            continue
        ranked = sorted(free, key=lambda h: (prefer.get(h, len(prefer)), -free[h].free_cores))
        host = next((h for h in ranked if _fits(pl, free[h], running.get(h, 0), needs)), None)
        out[job.label] = host
        if host is not None:
            take(host, needs)
    return out


# ssh


def _text(data: bytes | str | None) -> str:
    # TimeoutExpired carries bytes even in text mode.
    return data.decode(errors="replace") if isinstance(data, bytes) else (data or "")


def _probe_cmd(dirs: list[str]) -> str:
    quoted = " ".join(shlex.quote(d) for d in dirs)
    return (
        "id -un; nproc; cut -d' ' -f1 /proc/loadavg; awk '/^MemAvailable:/{print $2}' /proc/meminfo; "
        f"echo {_SEP}; for d in {quoted}; do "
        '[ -w "$d" ] && df -Pk "$d" | awk -v d="$d" \'NR==2{print d, $4}\'; done; '
        f"echo {_SEP}; ps -eo user:32=,comm=; "
        # The bracket keeps this shell and the grep out of the count.
        f"echo {_SEP}; ps -eww -o user:32=,args= | grep '[e]dr_driver.py'; true"
    )


def _parse_probe(host: str, out: str, tool_procs: str) -> HostProbe:
    sections: list[list[str]] = [[]]
    for line in out.splitlines():
        if line == _SEP:
            sections.append([])
        elif line.strip():
            sections[-1].append(line)
    if len(sections) != 4 or len(sections[0]) != 4:
        raise HostError(f"{host}: unreadable probe output: {out[:200]!r}")
    me, ncpu, load, mem_kb = (s.strip() for s in sections[0])
    scratch = [(int(kb), d) for d, kb in (ln.rsplit(None, 1) for ln in sections[1])]
    free_kb, mount = max(scratch) if scratch else (0, "")
    rx = re.compile(tool_procs) if tool_procs else None
    ours = others = 0
    for line in sections[2]:
        user, _, comm = line.strip().partition(" ")
        if rx and rx.search(comm.strip()):
            if user == me:
                ours += 1
            else:
                others += 1
    runs = sum(1 for ln in sections[3] if ln.split(None, 1)[0] == me)
    return HostProbe(
        host=host,
        free_cores=round(int(ncpu) - float(load), 1),
        free_ram_gb=round(int(mem_kb) / 2**20, 1),
        mount=mount,
        free_gb=round(free_kb / 2**20, 1),
        our_tool_procs=ours,
        other_tool_procs=others,
        our_runs=runs,
    )


class Ssh:
    """Runs short commands on a host with a timeout; `local` runs without ssh."""

    place = staticmethod(place)

    def __init__(self, site: Site) -> None:
        self.site = site

    def run(
        self, host: str, cmd: list[str] | str, timeout_s: float | None = None
    ) -> tuple[int, str, str]:
        """Run `cmd` on `host`; return (rc, stdout, stderr), rc 255 on a timeout."""
        if timeout_s is None:
            timeout_s = self.site.ssh_timeout_s
        if host == "local":
            argv = ["/bin/bash", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        else:
            text = cmd if isinstance(cmd, str) else shlex.join(cmd)
            argv = ["ssh", *self.site.ssh_options, host, "sh -c " + shlex.quote(text)]
        try:
            p = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            return TIMEOUT_RC, _text(e.stdout), _text(e.stderr) or f"timeout after {timeout_s}s"
        except OSError as e:
            return TIMEOUT_RC, "", str(e)
        return p.returncode, p.stdout, p.stderr

    def _run_ok(self, host: str, cmd: str) -> str:
        rc, out, err = self.run(host, cmd)
        if rc != 0:
            raise HostError(f"{host}: rc {rc}: {err.strip() or cmd}")
        return out

    def scratch_dirs(self, host: str) -> list[str]:
        """The scratch list of the host, or the site default."""
        h = self.site.hosts.get(host)
        return list(h.scratch) if h and h.scratch else list(self.site.scratch)

    def probe(self, host: str) -> HostProbe:
        """Free cores, RAM, the largest writable scratch and the tool processes of `host`."""
        out = self._run_ok(host, _probe_cmd(self.scratch_dirs(host)))
        return _parse_probe(host, out, self.site.tool_procs)

    def pid_alive(self, host: str, pid: int) -> bool:
        """True when `pid` runs on `host`; HostError when the host did not answer."""
        rc, _, err = self.run(host, f"ps -p {int(pid)} -o pid=")
        if rc not in (0, 1):
            raise HostError(f"{host}: rc {rc}: {err.strip()}")
        return rc == 0

    def kill_pgid(self, host: str, pgid: int, sig: str = "TERM") -> bool:
        """Signal the process group `pgid` on `host`; True when kill returned 0."""
        # pgid 0, 1 or a negative number would signal far more than one run.
        if not isinstance(pgid, int) or pgid <= 1:
            raise Refuse(f"'{pgid}' is not a process group id")
        if not _SIG_RE.match(sig):
            raise Refuse(f"'{sig}' is not a signal name")
        rc, _, err = self.run(host, f"kill -{sig} -- -{pgid}")
        if rc not in (0, 1):
            raise HostError(f"{host}: rc {rc}: {err.strip()}")
        return rc == 0

    def tool_processes(self, host: str, pattern: str) -> list[tuple[int, int, float, str, str]]:
        """Our processes on `host` whose comm matches `pattern`: (pid, etimes_s, pcpu, comm, args)."""
        out = self._run_ok(host, 'ps -ww -u "$(id -un)" -o pid=,etimes=,pcpu=,comm=,args=')
        rx = re.compile(pattern)
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            # ponytail: a comm with a space misaligns the row; such a comm never names a tool.
            if len(parts) == 5 and rx.search(parts[3]):
                rows.append((int(parts[0]), int(parts[1]), float(parts[2]), parts[3], parts[4]))
        return rows

    def check_local(self) -> list[str]:
        """The faults of the head node, as `local: ...` lines; see docs/requirements.md."""
        missing = [t for t in HEAD_TOOLS if shutil.which(t) is None]
        problems = [f"local: {t} not on PATH" for t in missing]
        if "local" not in self.site.hosts:  # else the host probe of the caller covers it
            try:
                self.probe("local")
            except HostError as e:
                problems.append(str(e))
        if "ps" not in missing and self.run("local", "ps -o etimes=,pcpu=,cputimes= -p $$")[0] != 0:
            problems.append("local: ps has no etimes, pcpu or cputimes column; procps-ng 3.3.10 or newer")
        return problems

    def scratch_free_gb(self, host: str, path: str) -> float:
        """Free GB of the filesystem under `path` on `host`."""
        out = self._run_ok(host, f"df -Pk {shlex.quote(path)} | awk 'NR==2{{print $4}}'")
        if not out.strip():
            raise HostError(f"{host}: df found nothing at {path}")
        return round(int(out.split()[-1]) / 2**20, 1)

"""The ssh wrapper, the host probe and the placement.

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
from typing import NamedTuple

from .guards import Refuse
from .model import Job, Needs, Placement, Project, Site

TIMEOUT_RC = 255
# What the head node runs itself: the controller calls the first four, the `local` host the rest.
HEAD_TOOLS = ("ssh", "rsync", "git", "python3", "nproc", "df", "ps", "awk", "stat", "readlink", "grep", "find")
_SEP = "@@"
_SIG_RE = re.compile(r"^[A-Z0-9]+$")


class HostError(OSError):
    """A host did not answer, or answered with text the probe cannot read."""


@dataclass
class HostProbe:
    """What one probe of a host found: its cores, RAM, scratch, GPUs and processes."""

    host: str
    free_cores: float
    free_ram_gb: float
    mount: str
    free_gb: float
    our_tool_procs: int = 0
    other_tool_procs: int = 0
    our_runs: int = 0
    cores: int = 0
    load: float = 0.0
    total_ram_gb: float = 0.0
    total_gb: float = 0.0
    gpus: int = 0
    gpus_idle: int = 0
    gpu_used_gb: float = 0.0
    gpu_total_gb: float = 0.0


# Placement


def _first_needs(project: Project, job: Job) -> Needs:
    names = job.stages or list(project.stages)
    return project.stages[names[0]].needs if names else Needs()


def job_tools(project: Project, job: Job) -> set[str]:
    """The tools the stages of a job need, over every stage it runs."""
    return {t for n in job.stages or list(project.stages) for t in project.stages[n].needs.tools}


def missing_tools(site: Site, host: str, tools: set[str]) -> list[str]:
    """The tools of `tools` that `host` lacks; a host outside the site table lacks none."""
    h = site.hosts.get(host)
    return sorted(t for t in tools if h is not None and not h.has(t))


def tool_versions(site: Site, host: str | None) -> dict[str, str]:
    """`tool.<name>.version` of every tool `host` has; "" when the site file gives no version."""
    h = site.hosts.get(host or "")
    have = h.tools if h and h.tools is not None else {n: "" for n in site.tools}
    return {f"tool.{n}.version": v for n, v in have.items()}


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
    """Give each job a host by the [placement] rules of edr.toml and the tools it needs; None means queue."""
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
        needs, tools = _first_needs(project, job), job_tools(project, job)
        if job.host != "auto":
            out[job.label] = job.host
            take(job.host, needs)
            continue
        ranked = sorted(free, key=lambda h: (prefer.get(h, len(prefer)), -free[h].free_cores))
        host = next((h for h in ranked if _fits(pl, free[h], running.get(h, 0), needs)
                     and not missing_tools(project.site, h, tools)), None)
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
        "id -un; nproc; cut -d' ' -f1 /proc/loadavg; "
        "awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print t; print a}' /proc/meminfo; "
        f"echo {_SEP}; for d in {quoted}; do "
        '[ -w "$d" ] && df -Pk "$d" | awk -v d="$d" \'NR==2{print d, $2, $4}\'; done; '
        f"echo {_SEP}; ps -eo user:32=,comm=; "
        # The bracket keeps this shell and the grep out of the count.
        f"echo {_SEP}; ps -eww -o user:32=,args= | grep '[e]dr_driver.py'; "
        f"echo {_SEP}; command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi "
        "--query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null; true"
    )


def _gpus(lines: list[str]) -> list[tuple[int, int, int]]:
    """(used MiB, total MiB, utilisation %) per well-formed nvidia-smi line."""
    out = []
    for ln in lines:
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            out.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return out


def _parse_probe(host: str, out: str, tool_procs: str) -> HostProbe:
    sections: list[list[str]] = [[]]
    for line in out.splitlines():
        if line == _SEP:
            sections.append([])
        elif line.strip():
            sections[-1].append(line)
    if len(sections) != 5 or len(sections[0]) != 5:
        raise HostError(f"{host}: unreadable probe output: {out[:200]!r}")
    me, ncpu, load, ram_kb, mem_kb = (s.strip() for s in sections[0])
    scratch = [(int(free), int(total), d) for d, total, free in (ln.rsplit(None, 2) for ln in sections[1])]
    free_kb, total_kb, mount = max(scratch) if scratch else (0, 0, "")
    gpus = _gpus(sections[4])
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
        # A load above the core count leaves no core free, not a negative count.
        free_cores=max(0.0, round(int(ncpu) - float(load), 1)),
        free_ram_gb=round(int(mem_kb) / 2**20, 1),
        mount=mount,
        free_gb=round(free_kb / 2**20, 1),
        our_tool_procs=ours,
        other_tool_procs=others,
        our_runs=runs,
        cores=int(ncpu),
        load=float(load),
        total_ram_gb=round(int(ram_kb) / 2**20, 1),
        total_gb=round(total_kb / 2**20, 1),
        gpus=len(gpus),
        gpus_idle=sum(1 for used, total, util in gpus if util < 5 and used < 0.05 * total),
        gpu_used_gb=round(sum(g[0] for g in gpus) / 1024, 1),
        gpu_total_gb=round(sum(g[1] for g in gpus) / 1024, 1),
    )


class Proc(NamedTuple):
    """One tool process: `edr` is True when its environment holds `EDR_RUN_ID`."""

    pid: int
    etimes: int
    pcpu: float
    comm: str
    args: str
    cwd: str = ""
    edr: bool = False


# grep and find skip the processes of other users: their environ and cwd are not readable.
_PROCS_CMD = (
    'ps -ww -u "$(id -un)" -o pid=,etimes=,pcpu=,comm=,args= || exit 1; '
    f"echo {_SEP}; grep -lsz '^EDR_RUN_ID=' /proc/[0-9]*/environ; "
    f"echo {_SEP}; find /proc -mindepth 2 -maxdepth 2 -name cwd -user \"$(id -un)\" -printf '%h %l\\n' 2>/dev/null; true"
)


class Ssh:
    """Runs short commands on a host with a timeout; `local` runs without ssh."""

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
        """Cores, RAM, the largest writable scratch, the GPUs and the tool processes of `host`, free and total."""
        out = self._run_ok(host, _probe_cmd(self.scratch_dirs(host)))
        return _parse_probe(host, out, self.site.tool_procs)

    def pids_alive(self, host: str, pids: list[int]) -> set[int]:
        """The pids of `pids` that run on `host`, by one `ps`; HostError when the host did not answer."""
        wanted = {int(p) for p in pids}
        rc, out, err = self.run(host, f"ps -p {','.join(str(p) for p in sorted(wanted))} -o pid=")
        if rc not in (0, 1):
            raise HostError(f"{host}: rc {rc}: {err.strip()}")
        return {int(t) for t in out.split() if t.isdigit()} & wanted

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

    def tool_processes(self, host: str, pattern: str) -> list[Proc]:
        """Our processes on `host` whose comm matches `pattern`, with their cwd and whether they carry `EDR_RUN_ID`.

        One ssh call: ps, the environ files that hold `EDR_RUN_ID`, and the cwd links of our processes."""
        out = self._run_ok(host, _PROCS_CMD)
        listing, edr, cwds = (out.split(f"\n{_SEP}\n") + ["", ""])[:3]
        with_env = {int(m) for m in re.findall(r"^/proc/(\d+)/environ$", edr, re.M)}
        cwd = {int(m[0]): m[1] for m in re.findall(r"^/proc/(\d+) (.*)$", cwds, re.M)}
        rx = re.compile(pattern)
        rows = []
        for line in listing.splitlines():
            parts = line.split(None, 4)
            # A comm with a space misaligns the row; no tool name has one.
            if len(parts) == 5 and rx.search(parts[3]):
                pid = int(parts[0])
                rows.append(Proc(pid, int(parts[1]), float(parts[2]), parts[3], parts[4], cwd.get(pid, ""),
                                 pid in with_env))
        return rows

    def check_local(self) -> list[str]:
        """The faults of the head node, as `local: ...` lines: a missing tool, or a ps without the columns."""
        # A site with only the host `local` never opens an ssh connection.
        needed = [t for t in HEAD_TOOLS if t != "ssh" or set(self.site.hosts) - {"local"}]
        missing = [t for t in needed if shutil.which(t) is None]
        problems = [f"local: {t} not on PATH" for t in missing]
        if "local" not in self.site.hosts:  # else the host probe of the caller covers it
            try:
                self.probe("local")
            except HostError as e:
                problems.append(str(e))
        if "ps" not in missing and self.run("local", "ps -o etimes=,pcpu=,cputimes= -p $$")[0] != 0:
            problems.append("local: ps has no etimes, pcpu or cputimes column; procps-ng 3.3.10 or newer")
        return problems

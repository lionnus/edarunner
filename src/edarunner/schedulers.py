"""The batch schedulers as backends: HTCondor, Slurm and LSF.

The scheduler picks the host and starts the driver as the job itself. Each scheduler has one pure
function that renders a `Request` into its submit input, and one backend class that runs the
scheduler commands through `[scheduler] submit_via` and maps the job states onto `Live`.
"""

from __future__ import annotations

import math
import re
import shlex
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .backend import Handle, Live, Request
from .hosts import HostError
from .model import Scheduler


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def _hmm(hours: float) -> str:
    minutes = math.ceil(hours * 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


# --- rendering

def condor_submit(req: Request) -> str:
    """The `condor_submit` file of one run; only syntax that HTCondor 8.4 reads."""
    lines = [
        "universe = vanilla",
        f"executable = {req.driver}",
        f"arguments = {req.spec}",
        "transfer_executable = false",
        "should_transfer_files = NO",
        f"output = {req.log}",
        f"error = {req.log.with_suffix('.err')}",
        f"log = {req.log.with_name(f'{req.run_id}.condor.log')}",
        f'+EdrRunId = "{req.run_id}"',
        f'+EdrProject = "{req.project}"',
        f"request_cpus = {req.cores}",
    ]
    if req.ram_gb:
        lines.append(f"request_memory = {math.ceil(req.ram_gb * 1024)}")
    if req.disk_gb:
        lines.append(f"request_disk = {math.ceil(req.disk_gb * 1024 * 1024)}")
    if req.hours:
        lines.append(f"periodic_remove = (RemoteWallClockTime - CumulativeSuspensionTime) > {math.ceil(req.hours * 3600)}")
    # A second start would run the stages again over the tree of the first; a person decides.
    lines.append("periodic_hold = NumJobStarts > 1")
    if req.licences:
        lines.append("concurrency_limits = " + ",".join(f"{n}:{k}" for n, k in sorted(req.licences.items())))
    if req.host:
        lines.append(f'requirements = (Machine == "{req.host}")')
    lines += ['environment = "EDR_SCHED_ID=$(ClusterId).$(ProcId)"', "getenv = false", *req.options, "queue"]
    return "\n".join(lines) + "\n"


def slurm_script(req: Request) -> str:
    """The `sbatch` script of one run: the `#SBATCH` header, then the driver."""
    opts = [f"--job-name={req.run_id}", f"--output={req.log}", "--nodes=1", f"--cpus-per-task={req.cores}"]
    if req.ram_gb:
        opts.append(f"--mem={math.ceil(req.ram_gb * 1024)}M")
    if req.disk_gb:
        opts.append(f"--tmp={math.ceil(req.disk_gb * 1024)}M")
    if req.hours:
        opts.append(f"--time={_hmm(req.hours)}:00")
    if req.licences:
        opts.append("--licenses=" + ",".join(f"{n}:{k}" for n, k in sorted(req.licences.items())))
    if req.host:
        opts.append(f"--nodelist={req.host}")
    if req.queue:
        opts.append(f"--partition={req.queue}")
    opts += ["--export=NONE", "--no-requeue", *req.options]
    return "\n".join(["#!/bin/sh", *(f"#SBATCH {o}" for o in opts),
                      f"exec {shlex.quote(str(req.driver))} {shlex.quote(str(req.spec))}"]) + "\n"


def lsf_argv(req: Request) -> list[str]:
    """The `bsub` argv of one run."""
    argv = ["bsub", "-J", req.run_id, "-o", str(req.log), "-n", str(req.cores), "-R", "span[hosts=1]"]
    if req.ram_gb:
        argv += ["-R", f"rusage[mem={_num(req.ram_gb)}GB]", "-M", f"{_num(req.ram_gb)}GB"]
    if req.disk_gb:
        argv += ["-R", f"rusage[tmp={_num(req.disk_gb)}GB]"]
    if req.hours:
        argv += ["-W", _hmm(req.hours)]
    if req.licences:
        argv += ["-R", "rusage[" + ":".join(f"{n}={k}" for n, k in sorted(req.licences.items())) + "]"]
    if req.host:
        argv += ["-m", req.host]
    if req.queue:
        argv += ["-q", req.queue]
    return [*argv, "-env", "none", "-rn", *req.options, "python3", str(req.driver), str(req.spec)]


# --- backends

class _Scheduler:
    """The commands of one scheduler, run through `submit_via`; the scheduler places, so `free` is None."""

    name = ""

    def __init__(self, sched: Scheduler) -> None:
        self.sched = sched

    def run(self, argv: list[str], timeout_s: int = 60) -> tuple[int, str, str]:
        via = self.sched.submit_via
        # ssh joins its arguments into one remote shell line, so the argv needs quotes there.
        full = [*via, shlex.join(argv)] if via and Path(via[0]).name == "ssh" else [*via, *argv]
        try:
            p = subprocess.run(full, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 255, "", str(e)
        return p.returncode, p.stdout, p.stderr

    def _ok(self, argv: list[str]) -> str:
        rc, out, err = self.run(argv)
        if rc != 0:
            raise HostError(f"{argv[0]}: rc {rc}: {(err or out).strip()}")
        return out

    def free(self, hosts: Iterable[str]) -> None:
        return None

    def file_host(self, run: dict[str, Any]) -> str:
        """The tree is under `tree_root`, which the head node reads in place."""
        return "local"

    def licence(self, name: str) -> bool | None:
        """True when the scheduler defines the licence `name`, False when it does not, None when it cannot say."""
        return None


class LsfBackend(_Scheduler):
    """LSF: `bsub` with the argv of `lsf_argv`, the handle `lsf:<jobid>`."""

    name = "lsf"
    STATES = {"PEND": Live.PENDING, "RUN": Live.RUNNING, "USUSP": Live.SUSPENDED, "SSUSP": Live.SUSPENDED,
              "PSUSP": Live.HELD, "DONE": Live.GONE, "EXIT": Live.GONE}

    def submit(self, req: Request) -> Handle:
        out = self._ok(lsf_argv(req))
        m = re.search(r"Job <(\d+)>", out)
        if not m:
            raise HostError(f"bsub: no job id in {out.strip()!r}")
        return Handle(self.name, m.group(1))

    def alive(self, handles: Iterable[Handle]) -> dict[Handle, tuple[Live, str]]:
        handles = list(handles)
        rc, out, err = self.run(["bjobs", "-noheader", "-o", "jobid stat", *(h.id for h in handles)])
        found = {p[0]: (self.STATES.get(p[1], Live.UNKNOWN), "") for p in (ln.split() for ln in out.splitlines())
                 if len(p) == 2 and p[0].isdigit()}
        # bjobs fails when one id is unknown and still lists the others.
        if rc != 0 and not found and "not found" not in (out + err):
            return {h: (Live.UNKNOWN, f"bjobs: rc {rc}: {err.strip()}") for h in handles}
        return {h: found.get(h.id, (Live.GONE, f"job {h.id} not found")) for h in handles}

    def stop(self, handle: Handle, hard: bool, pgids: Sequence[int] = ()) -> None:
        self._ok(["bkill", *(["-s", "KILL"] if hard else []), handle.id])


BY_NAME: dict[str, type[_Scheduler]] = {b.name: b for b in (LsfBackend,)}

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
    """The `sbatch` script of one run: the `#SBATCH` header, then the driver.

    It asks no `--tmp`: a node without `TmpDisk` in `slurm.conf` refuses every job that does, and the
    driver checks the free space of the tree itself."""
    opts = [f"--job-name={req.run_id}", f"--output={req.log}", "--nodes=1", f"--cpus-per-task={req.cores}"]
    if req.ram_gb:
        opts.append(f"--mem={math.ceil(req.ram_gb * 1024)}M")
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


class CondorBackend(_Scheduler):
    """HTCondor: the submit file next to the spec, the handle `condor:<cluster>.<proc>`."""

    name = "condor"
    STATES = {"1": Live.PENDING, "2": Live.RUNNING, "3": Live.GONE, "4": Live.GONE, "5": Live.HELD,
              "6": Live.RUNNING, "7": Live.SUSPENDED}

    def submit(self, req: Request) -> Handle:
        sub = req.spec.with_name(f"{req.run_id}.sub")
        sub.write_text(condor_submit(req))
        out = self._ok(["condor_submit", "-terse", str(sub)])
        ident = out.split()[0] if out.split() else ""
        if not re.fullmatch(r"\d+\.\d+", ident):
            raise HostError(f"condor_submit: no job id in {out.strip()!r}")
        return Handle(self.name, ident)

    def alive(self, handles: Iterable[Handle]) -> dict[Handle, tuple[Live, str]]:
        handles = list(handles)
        rc, out, err = self.run(["condor_q", *(h.id for h in handles), "-af", "ClusterId", "ProcId", "JobStatus",
                                 "HoldReason"])
        if rc != 0:
            return {h: (Live.UNKNOWN, f"condor_q: rc {rc}: {err.strip()}") for h in handles}
        found = {}
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3:
                reason = parts[3] if len(parts) > 3 and parts[2] == "5" else ""
                found[f"{parts[0]}.{parts[1]}"] = (self.STATES.get(parts[2], Live.UNKNOWN), reason)
        return {h: found.get(h.id, (Live.GONE, f"job {h.id} left the queue")) for h in handles}

    def stop(self, handle: Handle, hard: bool, pgids: Sequence[int] = ()) -> None:
        """`condor_rm`: the starter sends SIGTERM, and SIGKILL after the job's kill timeout."""
        self._ok(["condor_rm", handle.id])

    def licence(self, name: str) -> bool | None:
        rc, out, err = self.run(["condor_config_val", "-negotiator", f"{name.upper()}_LIMIT"])
        if rc == 0 and out.strip():
            return True
        return False if "not defined" in (out + err).lower() else None


class SlurmBackend(_Scheduler):
    """Slurm: the script next to the spec, the handle `slurm:<jobid>`."""

    name = "slurm"
    STATES = {"PENDING": Live.PENDING, "CONFIGURING": Live.PENDING, "RUNNING": Live.RUNNING,
              "COMPLETING": Live.RUNNING, "SUSPENDED": Live.SUSPENDED}

    def submit(self, req: Request) -> Handle:
        script = req.spec.with_name(f"{req.run_id}.sbatch")
        script.write_text(slurm_script(req))
        ident = self._ok(["sbatch", "--parsable", str(script)]).strip().split(";")[0]
        if not ident.isdigit():
            raise HostError(f"sbatch: no job id in {ident!r}")
        return Handle(self.name, ident)

    def _state(self, state: str, reason: str) -> Live:
        if state == "PENDING" and reason.startswith("JobHeld"):
            return Live.HELD
        return self.STATES.get(state, Live.GONE)

    def alive(self, handles: Iterable[Handle]) -> dict[Handle, tuple[Live, str]]:
        """One `squeue`; `sacct` for the jobs that left the queue."""
        handles = list(handles)
        ids = ",".join(h.id for h in handles)
        rc, out, err = self.run(["squeue", "-h", "-o", "%i %T %r", "-j", ids])
        found: dict[str, tuple[Live, str]] = {}
        for line in out.splitlines() if rc == 0 else []:
            parts = line.split(None, 2)
            if len(parts) >= 2:
                found[parts[0]] = (self._state(parts[1], parts[2] if len(parts) > 2 else ""), "")
        missing = [h.id for h in handles if h.id not in found]
        if missing:
            rc2, out2, err2 = self.run(["sacct", "-n", "-P", "-X", "-o", "JobID,State", "-j", ",".join(missing)])
            if rc != 0 and rc2 != 0:
                why = f"squeue: rc {rc}: {err.strip()}; sacct: rc {rc2}: {err2.strip()}"
                return {h: found.get(h.id, (Live.UNKNOWN, why)) for h in handles}
            for line in out2.splitlines() if rc2 == 0 else []:
                parts = line.split("|")
                if len(parts) >= 2 and parts[0] in missing:
                    state = parts[1].split()[0] if parts[1].split() else ""
                    found[parts[0]] = (self._state(state, ""), "" if state in self.STATES else state)
        return {h: found.get(h.id, (Live.GONE, f"job {h.id} left the queue")) for h in handles}

    def stop(self, handle: Handle, hard: bool, pgids: Sequence[int] = ()) -> None:
        """`scancel`: SIGTERM, and SIGKILL after `KillWait`; `hard` sends SIGKILL to the batch script at once."""
        self._ok(["scancel", *(["-f", "-s", "KILL"] if hard else []), handle.id])

    def licence(self, name: str) -> bool | None:
        rc, out, err = self.run(["scontrol", "show", "lic", name])
        if rc == 0 and f"LicenseName={name}" in out:
            return True
        return False if rc == 0 or "not found" in (out + err).lower() else None


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


BY_NAME: dict[str, type[_Scheduler]] = {b.name: b for b in (CondorBackend, SlurmBackend, LsfBackend)}

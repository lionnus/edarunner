#!/usr/bin/env python3
"""Drive one run from its spec: python3 edr_driver.py <spec.json>.

Python 3.6, standard library only, no import of the package.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import string
import subprocess
import sys
import threading
import time
import traceback
from typing import Optional  # noqa: F401  (the type comments use it)

POLL_S = 5
TICK_S = 0.5
DU_EVERY_S = 600
TAIL_LINES = 80
GB = 1024.0 ** 3


def identity(spec, env):
    # type: (dict, dict) -> dict
    """The host the driver runs on, when the spec names none, and the job id of a scheduler."""
    sched = env.get("EDR_SCHED_ID") or env.get("SLURM_JOB_ID") or env.get("LSB_JOBID")
    return {"host": spec.get("host") or socket.gethostname(), "sched_id": sched or None}


def group_alive(pgid):
    # type: (int) -> bool
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def usage(pgids):
    # type: (list) -> tuple
    """(CPU seconds, RSS in GB) of every process in `pgids`, from /proc, else from ps; (None, None) when ps fails.

    The CPU seconds from /proc include the reaped children.
    """
    if not pgids:
        return 0.0, 0.0
    want = set(pgids)
    if not os.path.isdir("/proc/self"):
        try:
            out = subprocess.run(["ps", "-e", "-o", "pgid=,cputimes=,rss="], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                                 timeout=30).stdout
            rows = [[int(x) for x in ln.split()] for ln in out.splitlines() if len(ln.split()) == 3]
        except (OSError, ValueError, subprocess.SubprocessError):
            return None, None
        rows = [r for r in rows if r[0] in want]
        return float(sum(r[1] for r in rows)), round(sum(r[2] for r in rows) / (1024.0 * 1024.0), 3)
    ticks, pages, hz, page = 0, 0, float(os.sysconf("SC_CLK_TCK")), os.sysconf("SC_PAGE_SIZE")
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as f:
                fields = f.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # After the comm: state, ppid, pgrp, ..., utime, stime, cutime, cstime at 11 to 14, rss at 21.
        if int(fields[2]) in want:
            ticks += sum(int(x) for x in fields[11:15])
            pages += int(fields[21])
    return round(ticks / hz, 2), round(pages * page / GB, 3)


def log_size(path):
    # type: (str) -> Optional[int]
    """The size of a log in bytes, or None when it does not exist."""
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


class Fail(Exception):
    """A terminal phase: args are (exit code, phase)."""


class Killed(Exception):
    """A signal or a stop file ended the run."""


def free_gb(path):
    # type: (str) -> Optional[float]
    try:
        return round(shutil.disk_usage(path).free / GB, 3)
    except OSError:
        return None


def du_gb(path):
    # type: (str) -> Optional[float]
    try:
        out = subprocess.run(["du", "-sk", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=600).stdout
        return round(int(out.split()[0]) / (1024.0 * 1024.0), 3)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def tail(path, n, size=65536):
    # type: (str, int, int) -> str
    """Return the last n lines of a file as one string."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def read_leases(d):
    # type: (str) -> list
    """Every lease in a lease directory as (file name, content); a temporary file is skipped."""
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if n.startswith("."):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                out.append((n, json.load(f)))
        except OSError:
            pass  # released since the listing
        except ValueError:
            out.append((n, {}))
    return out


def digest(root, names):
    # type: (str, list) -> dict
    """The sha256 of each named file under `root`; None for a file that does not exist."""
    out = {}
    for n in names:
        try:
            with open(os.path.join(root, n), "rb") as f:
                out[n] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            out[n] = None
    return out


def signature(path):
    # type: (str) -> str
    """Return the last log line with digits removed."""
    lines = [l for l in tail(path, 20).splitlines() if l.strip()]
    return re.sub(r"\d+", "", lines[-1]).strip() if lines else ""


class Driver(object):
    """One run: stages, task groups, heartbeat, limits, signals."""

    def __init__(self, spec_path):
        # type: (str) -> None
        with open(spec_path) as f:
            spec = json.load(f)
        self.spec = spec
        self.run_id = spec["run_id"]
        self.project = spec["project"]
        # The lease key of the run: a run id is unique in its project only.
        self.key = "%s.%s" % (self.project, self.run_id)
        self.root = spec["root"]
        self.state_file = spec["state_file"]
        self.queue_dir = spec["queue_dir"]
        self.stages = spec["stages"]
        self.shell = spec.get("shell") or "/bin/bash"
        self.limits = spec.get("limits") or {}
        self.env = dict(os.environ)
        # A value such as "/opt/eda/bin:$PATH" names the host's own variables, so it
        # expands here, on the host, against the environment the driver started with.
        for k, v in (spec.get("env") or {}).items():
            self.env[k] = string.Template(str(v)).safe_substitute(self.env)
        if not os.path.isdir(self.root):
            raise ValueError("root is not a directory: " + self.root)
        spec_dir = os.path.dirname(os.path.abspath(spec_path))
        self.stop_file = os.path.join(spec_dir, self.run_id + ".stop")
        self.keep_file = os.path.join(spec_dir, self.run_id + ".keep.json")
        self.lock = threading.RLock()
        self.procs = {}  # pgid -> Popen
        self.signalled = {}  # pgid -> Popen of every group kill_all reached; its shell may exit first
        self.killed = None
        self.stop_mode = None
        self.over_budget = None
        self.sigs = []
        self.leases = {}  # key -> [lease path]
        self.stop_evt = threading.Event()
        self.last_cpu = None  # (time, CPU seconds) of the last sample
        now = int(time.time())
        self.hb = {
            "schema": 1, "run_id": self.run_id, "batch": spec.get("batch"),
            "label": spec.get("label"), "config": spec.get("config"),
            "host": spec.get("host"), "root": self.root, "driver_pid": os.getpid(),
            "pgids": [], "phase": "setup", "stage": None, "step": None, "step_name": None,
            "tasks": {}, "stages": {}, "step_times": {}, "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 0, "queued": 0},
            "started": now, "updated": now, "elapsed_s": 0, "disk_free_gb": None,
            "tree_gb": None, "cpu_pct": None, "rss_gb": None, "exit": None, "killed_by": None, "last_cmd": None,
            "last_log": None, "log": None}
        self.hb.update(identity(spec, os.environ))

    # heartbeat

    def beat(self, tree=False):
        # type: (bool) -> None
        """Write the heartbeat by an atomic rename; `tree` also runs du."""
        free = free_gb(self.root)
        tree = du_gb(self.root) if tree else None
        samples = self.samples()
        with self.lock:
            hb = self.hb
            hb.update(samples)
            now = int(time.time())
            hb["updated"] = now
            hb["elapsed_s"] = now - hb["started"]
            hb["pgids"] = sorted(self.procs)
            hb["disk_free_gb"] = free
            if tree is not None:
                hb["tree_gb"] = tree
            if hb["log"]:
                hb["last_log"] = tail(hb["log"], 3)
            hb["keep_hours"] = self.read_keep()[1]
            hb["counts"]["running"] = sum(1 for t in hb["tasks"].values() if t["phase"] == "running")
            data = json.dumps(hb, sort_keys=True)
            # Two threads share the tmp name; the lock keeps the newest snapshot last.
            tmp = "%s.%d.tmp" % (self.state_file, os.getpid())
            try:
                with open(tmp, "w") as f:
                    f.write(data)
                os.rename(tmp, self.state_file)
            except OSError as e:
                sys.stderr.write("heartbeat write failed: %s\n" % e)

    def samples(self):
        # type: () -> dict
        """The CPU time and RSS of the process groups, the CPU use since the last sample, and the size of the log.

        `cpu_s` and `log_bytes` are what the watcher's hung check compares.
        """
        with self.lock:
            pgids, log = sorted(self.procs), self.hb["log"]
        cpu, rss = usage(pgids)
        now = time.time()
        out = {"cpu_s": cpu, "rss_gb": rss, "log_bytes": log_size(log)}
        with self.lock:
            last = self.last_cpu
            if cpu is None:
                out["cpu_pct"], self.last_cpu = None, None
            elif not last or now - last[0] >= 1:
                # A process group that ended takes its CPU seconds with it, so a drop reads as zero.
                out["cpu_pct"] = round(max(0.0, (cpu - last[1]) / (now - last[0]) * 100), 1) if last else None
                self.last_cpu = (now, cpu)
        return out

    def beat_loop(self):
        period = float(self.limits.get("heartbeat_s") or 60)
        last_du = 0.0
        while not self.stop_evt.wait(period):
            du = time.time() - last_du >= DU_EVERY_S
            if du:
                last_du = time.time()
            self.beat(tree=du)

    def set_phase(self, phase, **fields):
        with self.lock:
            self.hb["phase"] = phase
            self.hb.update(fields)
        self.beat()

    def record_stage(self, name, status, **fields):
        with self.lock:
            e = self.hb["stages"].setdefault(name, {
                "status": status, "attempt": 1, "started": None, "ended": None, "exit": None, "log": None})
            e["status"] = status
            e.update(fields)
        self.beat()

    def record_task(self, tid, phase, **fields):
        with self.lock:
            e = self.hb["tasks"].setdefault(tid, {
                "phase": phase, "pid": None, "pgid": None, "started": None,
                "ended": None, "exit": None, "signature": None, "log": None})
            e["phase"] = phase
            e.update(fields)
        self.beat()

    def read_keep(self):
        # type: () -> tuple
        """Return (mtime, hours) of the keep file, or (0, 0.0)."""
        try:
            with open(self.keep_file) as f:
                hours = float(json.load(f).get("hours") or 0)
            return os.path.getmtime(self.keep_file), hours
        except (OSError, ValueError, AttributeError):
            return 0, 0.0

    def keep_extra(self, started):
        # type: (int) -> float
        # A keep written before the stage or task started belongs to an earlier one.
        mtime, hours = self.read_keep()
        return hours if mtime >= started else 0.0

    # signals and the stop file

    def on_signal(self, signum, frame):
        if self.killed:
            return
        self.killed = signal.Signals(signum).name
        with self.lock:
            self.hb["killed_by"] = self.killed
        self.kill_all(signum)

    def kill_all(self, sig):
        with self.lock:
            self.signalled.update(self.procs)
        for pgid in list(self.procs):
            self.killpg(pgid, sig)

    @staticmethod
    def killpg(pgid, sig):
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass

    def read_stop(self):
        # type: () -> Optional[str]
        try:
            with open(self.stop_file) as f:
                return f.read().strip() or "now"
        except OSError:
            return None

    def check_stop(self):
        """Raise Killed after a signal or a stop file with content "now"."""
        if self.killed:
            raise Killed(self.killed)
        mode = self.read_stop()
        if mode and mode != self.stop_mode:
            self.stop_mode = mode
            with self.lock:
                self.hb["stop"] = mode
        if mode == "now":
            with self.lock:
                self.hb["killed_by"] = "stop"
            self.kill_all(signal.SIGTERM)
            raise Killed("stop")

    def wait(self, seconds):
        end = time.time() + seconds
        while True:
            self.check_stop()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, TICK_S))

    # processes

    def log_path(self, name):
        return os.path.join(self.root, "log", name + ".log")

    def spawn(self, cmd, cwd, log):
        # type: (str, str, str) -> subprocess.Popen
        """Start a command in its own session with the log appended."""
        with self.lock:
            self.hb["last_cmd"] = cmd
        with open(log, "ab") as fh:
            fh.write(("# edr: %s\n" % cmd).encode())
            fh.flush()  # the header goes before the output of the command
            p = subprocess.Popen([self.shell, "-c", cmd], cwd=cwd, env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=fh,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        with self.lock:
            self.procs[p.pid] = p
        return p

    def forget(self, p):
        with self.lock:
            self.procs.pop(p.pid, None)

    def run_wait(self, cmd, cwd, log):
        # type: (str, str, str) -> int
        p = self.spawn(cmd, cwd, log)
        while p.poll() is None:
            self.check_stop()
            time.sleep(TICK_S)
        self.forget(p)
        self.check_stop()
        return p.returncode

    def sh(self, cmd, cwd, timeout=120):
        # type: (object, str, int) -> tuple
        """Run a probe, a shell string or an argv list; return (rc, output), or (None, "") when it fails."""
        argv = cmd if isinstance(cmd, list) else [self.shell, "-c", cmd]
        try:
            r = subprocess.run(argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=timeout)
            return r.returncode, r.stdout
        except (OSError, subprocess.SubprocessError):
            return None, ""

    def reap(self, timeout):
        """Wait up to `timeout` for every recorded process group to end, then SIGKILL the groups.

        The test is the group, not the direct child: a tool that ignores SIGTERM outlives its shell."""
        end = time.time() + timeout
        with self.lock:
            procs = list({**self.signalled, **self.procs}.items())
        for pgid, p in procs:
            while time.time() < end and (p.poll() is None or group_alive(pgid)):
                time.sleep(TICK_S)
        for pgid, p in procs:
            self.killpg(pgid, signal.SIGKILL)
            p.wait()
            self.forget(p)

    # limits

    def free_seats(self, tool):
        # type: (dict) -> Optional[int]
        """The first number the probe of a tool prints, or None when the probe fails."""
        rc, out = self.sh(tool["probe"], self.root)
        try:
            free = int(out.split("\n", 1)[0].split()[0]) if rc == 0 else None
        except (IndexError, ValueError):
            free = None
        if free is None:
            sys.stderr.write("tool probe failed for %s: rc=%s %s\n" % (tool["name"], rc, out.strip()[:200]))
        return free

    def others(self, tool, key, now):
        # type: (dict, str, float) -> list
        """The leases of a tool that count against `key`: those of another key, younger than lease_s.

        After lease_s the tool holds its seat, and the probe no longer reports it free."""
        lease_s = float(self.limits.get("lease_s") or 600)
        return [(float(c.get("ts") or 0), n) for n, c in read_leases(tool["leases"])
                if n.rsplit(".", 1)[0] != key and now - float(c.get("ts") or 0) < lease_s]

    def take(self, tools, key, budget):
        # type: (list, str, dict) -> Optional[str]
        """Lease the seats of every tool under `key`; return why the caller waits, or None.

        One file per seat, <key>.<n>, made by a rename. After the rename the driver counts again and
        backs off when an older lease of another run leaves too few seats, so of two drivers that
        saw the same free seat, the later one waits."""
        hours = budget.get("hours")
        for t in tools:
            seats, free = int(t.get("seats") or 1), self.free_seats(t)
            if not t.get("leases"):
                if free is not None and free < seats:
                    return "%s: %d free, 0 held by others, %d needed" % (t["name"], free, seats)
                continue
            held = self.others(t, key, time.time())
            if free is None or free - len(held) >= seats:
                ts = self.lease(t, key, seats, hours)
                held = [h for h in self.others(t, key, time.time()) if h < (ts, key)]
            if free is not None and free - len(held) < seats:
                self.release(key)
                return "%s: %d free, %d held by others, %d needed" % (t["name"], free, len(held), seats)
        return None

    def lease(self, tool, key, seats, hours):
        # type: (dict, str, int, object) -> float
        """Write `seats` lease files <key>.<n> by a temporary file and a rename; return their time."""
        ts = time.time()
        body = json.dumps({"project": self.project, "run_id": self.run_id, "key": key, "stage": self.hb.get("stage"), "pid": os.getpid(),
                           "host": self.spec.get("host"), "ts": ts,
                           "budget_s": float(hours) * 3600 if hours is not None else None})
        d = tool["leases"]
        os.makedirs(d, exist_ok=True)
        for n in range(seats):
            path = os.path.join(d, "%s.%d" % (key, n))
            tmp = os.path.join(d, ".%s.%d.tmp" % (key, n))
            with open(tmp, "w") as f:
                f.write(body)
            os.rename(tmp, path)
            self.leases.setdefault(key, []).append(path)
        return ts

    def release(self, key=None):
        # type: (str) -> None
        """Remove the lease files of `key`, or of every key."""
        for k in [key] if key else list(self.leases):
            for path in self.leases.pop(k, []):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def gate(self, st):
        """Wait until the stage's tools have the seats, and lease them under <project>.<run_id>.<stage>."""
        tools = st.get("tools")
        if not tools:
            return
        name = st["name"]
        self.set_phase("gate:" + name, stage=name)
        t0, last = time.time(), None
        gate_max = float(self.limits.get("gate_max_s") or 0)
        while True:
            why = self.take(tools, self.key + "." + name, st.get("budget") or {})
            if why != last:
                sys.stderr.write("gate %s: %s\n" % (name, "wait for " + why if why else "open"))
                last = why
                with self.lock:
                    self.hb["gate"] = why
            if not why:
                return
            left = gate_max - (time.time() - t0)
            if left <= 0:
                raise Fail(4, "FAILED:" + name)
            self.wait(min(POLL_S, left))

    def host_full(self):
        # type: () -> bool
        free = free_gb(self.root)
        full = free is not None and free < float(self.limits.get("host_free_min_gb") or 0)
        with self.lock:
            self.hb["host_full"] = full
        return full

    def budget_over(self, budget, started):
        # type: (dict, int) -> Optional[str]
        hours = budget.get("hours")
        if hours is not None and time.time() - started > (float(hours) + self.keep_extra(started)) * 3600:
            return "hours"
        disk, tree = budget.get("disk_gb"), self.hb.get("tree_gb")
        if disk is not None and tree is not None and tree > float(disk):
            return "disk"
        return None

    def mark_over(self, name, budget, procs):
        self.over_budget = name
        with self.lock:
            self.hb["over_budget"] = name
        if budget.get("kill"):
            for p in procs:
                self.killpg(p.pid, signal.SIGTERM)

    def progress(self, st, cwd):
        if not st.get("progress"):
            return
        rc, out = self.sh(st["progress"], cwd, timeout=30)
        m = re.search(r"\d+", out or "")
        if rc is None or not m:
            return
        step, steps = int(m.group(0)), st.get("steps") or []
        with self.lock:
            # The first time the driver sees a step is the time that step started, to within POLL_S.
            self.hb["step_times"].setdefault(st["name"], {}).setdefault(str(step), int(time.time()))
            self.hb["step"] = step
            self.hb["step_name"] = steps[min(step, len(steps) - 1)] if steps else None

    # the runtime step

    def setup_runtime(self):
        """Run `[runtime] setup` in the tree root; skip it when the stamp of the last setup still holds."""
        rt = self.spec.get("runtime") or {}
        cmd = rt.get("setup")
        if not cmd:
            return
        watched = rt.get("when_changed") or []
        stamp_file = os.path.join(self.root, ".edr-runtime")
        stamp = {"setup": cmd, "files": digest(self.root, watched)}
        if watched:
            try:
                with open(stamp_file) as f:
                    if json.load(f) == stamp:
                        sys.stderr.write("runtime: %s unchanged, setup skipped\n" % ", ".join(watched))
                        now = int(time.time())
                        self.record_stage("setup", "skipped", started=now, ended=now)
                        return
            except (OSError, ValueError):
                pass
        log = self.log_path("setup")
        self.set_phase("setup", log=log)
        self.record_stage("setup", "running", started=int(time.time()), log=log)
        rc = self.run_wait(cmd, self.root, log)
        self.record_stage("setup", "failed" if rc else "done", ended=int(time.time()), exit=rc)
        if rc:
            raise Fail(5, "FAILED:runtime")
        with open(stamp_file, "w") as f:
            json.dump(stamp, f)

    # stages

    def run_stage(self, st, checkpoint):
        # type: (dict, str) -> None
        name = st["name"]
        cwd = os.path.join(self.root, st.get("cwd") or ".")
        cmd = st.get("cmd") or ""
        if checkpoint:
            # The full cmd would delete the checkpoints the resume needs.
            if not st.get("resume"):
                sys.stderr.write("stage %s has no resume for checkpoint %s\n" % (name, checkpoint))
                raise Fail(2, "FAILED:" + name)
            cmd = st["resume"].replace("{checkpoint}", str(checkpoint))
        log = self.log_path(name)
        retry, budget = st.get("retry") or {}, st.get("budget") or {}
        attempt = 0
        while True:
            phase = "stage:" + name if not attempt else "retry:%s:%d" % (name, attempt)
            self.set_phase(phase, stage=name, log=log, step=None, step_name=None)
            started = int(time.time())
            self.record_stage(name, "running", attempt=attempt + 1, started=started, ended=None, exit=None, log=log)
            p = self.spawn(cmd, cwd, log)
            next_probe = 0.0
            while p.poll() is None:
                self.check_stop()
                if time.time() >= next_probe:
                    self.progress(st, cwd)
                    next_probe = time.time() + POLL_S
                if not self.over_budget and self.budget_over(budget, started):
                    self.mark_over(name, budget, [p])
                time.sleep(TICK_S)
            self.forget(p)
            self.check_stop()
            self.progress(st, cwd)
            ended = {"ended": int(time.time()), "exit": p.returncode}
            if self.over_budget:
                self.record_stage(name, "over_budget", **ended)
                raise Fail(9, "OVER_BUDGET:" + name)
            if p.returncode == 0:
                self.record_stage(name, "done", **ended)
                return
            self.record_stage(name, "failed", **ended)
            attempt += 1
            if (retry.get("match") and attempt <= int(retry.get("max") or 0)
                    and re.search(retry["match"], tail(log, TAIL_LINES))):
                self.set_phase("retry:%s:%d" % (name, attempt))
                self.wait(float(retry.get("wait_s") or 0))
                continue
            raise Fail(5, "FAILED:" + name)

    def pending(self, q, tasks, skipped):
        # type: (str, list, set) -> list
        names = set(os.listdir(os.path.join(q, "pending")))
        return [t["id"] for t in tasks if t["id"] in names and t["id"] not in skipped]

    def end_task(self, q, st, task, rc, log, cwd):
        tid = task["id"]
        sig = signature(log) if rc else None
        try:
            os.rename(os.path.join(q, "claimed", tid + "." + self.run_id), os.path.join(q, "done", tid))
        except OSError:
            pass
        after = st.get("after_each")
        if after and "{task_dir}" in after and not task.get("dir"):
            sys.stderr.write("after_each skipped for %s: no dir\n" % tid)
        elif after:
            self.run_wait(after.replace("{task_dir}", task.get("dir") or ""), cwd, log)
        self.release("%s.%s.%s" % (self.key, st["name"], tid))
        streak = int(self.limits.get("streak") or 0)
        with self.lock:
            c = self.hb["counts"]
            if rc == 0:
                c["done"] += 1
                self.sigs = []
            else:
                c["failed"] += 1
                self.sigs.append(sig)
                if streak and len(self.sigs) >= streak and len(set(self.sigs[-streak:])) == 1:
                    self.hb["looping"] = True
        self.record_task(tid, "done" if rc == 0 else "failed", ended=int(time.time()), exit=rc, signature=sig)

    def run_group(self, st):
        name = st["name"]
        cwd = os.path.join(self.root, st.get("cwd") or ".")
        self.set_phase("group:" + name, stage=name, log=None, step=None, step_name=None)
        if st.get("prepare"):
            log = self.log_path(name + ".prepare")
            with self.lock:
                self.hb["log"] = log
            if self.run_wait(st["prepare"], cwd, log):
                raise Fail(5, "FAILED:" + name)
        q = os.path.join(self.queue_dir, name)
        for d in ("pending", "claimed", "done"):
            os.makedirs(os.path.join(q, d), exist_ok=True)
        tasks = st.get("tasks") or []
        by_id = dict((t["id"], t) for t in tasks)
        # A shard that joins later must not re-create a task another shard holds:
        # a claim is claimed/<id>.<run id>, so the prefix names it.
        claimed = set(n.split(".", 1)[0] for n in os.listdir(os.path.join(q, "claimed")))
        for t in tasks:
            if os.path.exists(os.path.join(q, "done", t["id"])) or t["id"] in claimed:
                continue
            try:
                os.close(os.open(os.path.join(q, "pending", t["id"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            except FileExistsError:
                pass
        parallel = max(1, int(st.get("parallel") or 1))
        tools, budget = st.get("tools") or [], st.get("budget") or {}
        per_task = budget.get("per") == "task"
        running = {}  # id -> (proc, task, started, log)
        skipped = set()
        self.sigs = []
        with self.lock:
            self.hb.pop("looping", None)
        gate_until, t_group = 0.0, int(time.time())
        while True:
            self.check_stop()
            for tid in list(running):
                p, task, t0, log = running[tid]
                if p.poll() is None:
                    tb = task.get("budget") or (budget if per_task else {})
                    if not self.hb["tasks"][tid].get("over_budget") and self.budget_over(tb, t0) == "hours":
                        self.record_task(tid, "running", over_budget=True)
                        self.killpg(p.pid, signal.SIGTERM)
                    continue
                del running[tid]
                self.forget(p)
                self.end_task(q, st, task, p.returncode, log, cwd)
            if not per_task and not self.over_budget and self.budget_over(budget, t_group):
                self.mark_over(name, budget, [r[0] for r in running.values()])
            hold = bool(self.stop_mode or self.over_budget or self.hb.get("looping"))
            pending = self.pending(q, tasks, skipped)
            with self.lock:
                self.hb["counts"]["queued"] = len(pending)
            if not running and (hold or not pending):
                return
            while pending and len(running) < parallel and not hold and time.time() >= gate_until:
                if self.host_full():
                    break
                tid = pending.pop(0)
                task = by_id[tid]
                need = float((task.get("needs") or st.get("needs") or {}).get("disk_gb") or 0)
                free = free_gb(self.root)
                if free is not None and free < need:
                    skipped.add(tid)
                    with self.lock:
                        self.hb["counts"]["skipped"] += 1
                    self.record_task(tid, "skipped")
                    continue
                key = "%s.%s.%s" % (self.key, name, tid)
                why = self.take(task.get("tools") or tools, key, task.get("budget") or budget)
                with self.lock:
                    self.hb["gate"] = why
                if why:
                    gate_until = time.time() + POLL_S
                    break
                try:
                    os.rename(os.path.join(q, "pending", tid), os.path.join(q, "claimed", tid + "." + self.run_id))
                except OSError:
                    self.release(key)
                    continue
                log = self.log_path("%s.%s" % (name, tid))
                p = self.spawn(task["cmd"], cwd, log)
                running[tid] = (p, task, int(time.time()), log)
                with self.lock:
                    self.hb["log"] = log
                self.record_task(tid, "running", pid=p.pid, pgid=p.pid, started=running[tid][2], log=log)
            time.sleep(TICK_S)

    # the run

    def main(self):
        # type: () -> tuple
        """Run every stage from start_at; return (exit code, phase)."""
        names = [s["name"] for s in self.stages]
        start_at = self.spec.get("start_at") or {}
        start = start_at.get("stage") or (names[0] if names else None)
        if start not in names:
            sys.stderr.write("start stage %s not in %s\n" % (start, names))
            raise Fail(2, "FAILED:setup")
        todo = self.stages[names.index(start):]
        need = float((todo[0].get("needs") or {}).get("disk_gb") or 0)
        free = free_gb(self.root)
        if free is not None and free < need:
            sys.stderr.write("%.1f GB free, %s needs %.1f\n" % (free, todo[0]["name"], need))
            raise Fail(3, "FAILED:" + todo[0]["name"])
        self.setup_runtime()
        for i, st in enumerate(todo):
            while self.host_full():
                self.wait(POLL_S)
            self.gate(st)
            try:
                if "tasks" in st:
                    # A task takes its own lease; the stage lease only opened the gate.
                    self.release(self.key + "." + st["name"])
                    self.record_stage(st["name"], "running", attempt=1, started=int(time.time()), ended=None, exit=None)
                    self.run_group(st)
                    self.record_stage(st["name"], "done", ended=int(time.time()), exit=0)
                else:
                    self.run_stage(st, start_at.get("checkpoint") if i == 0 else None)
            finally:
                self.release()
            if self.over_budget:
                raise Fail(9, "OVER_BUDGET:" + st["name"])
            if self.stop_mode:
                raise Fail(10, "STOPPED")
        c = self.hb["counts"]
        if c["failed"] or c["skipped"]:
            return 8, "INCOMPLETE:%df%ds" % (c["failed"], c["skipped"])
        return 0, "done"

    def run(self):
        # type: () -> int
        """Run to a terminal phase and return the exit code."""
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(s, self.on_signal)
        os.makedirs(os.path.join(self.root, "log"), exist_ok=True)
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        self.beat()
        t = threading.Thread(target=self.beat_loop, daemon=True)
        t.start()
        try:
            code, phase = self.main()
        except Fail as e:
            code, phase = e.args
        except Killed:
            code = 10
            phase = "STOPPED" if os.path.exists(self.stop_file) else "KILLED:" + str(self.killed)
            self.kill_all(signal.Signals[self.killed] if self.killed else signal.SIGTERM)
        except Exception:
            traceback.print_exc()
            self.kill_all(signal.SIGTERM)
            code, phase = 5, "FAILED:" + str(self.hb.get("stage") or "setup")
        self.reap(10)
        self.release()
        self.stop_evt.set()
        t.join(30)
        with self.lock:
            self.hb["exit"], self.hb["phase"] = code, phase
        self.beat()
        return code


def main(argv):
    # type: (list) -> int
    if len(argv) != 2:
        sys.stderr.write("usage: edr_driver.py <spec.json>\n")
        return 2
    try:
        driver = Driver(argv[1])
    except (OSError, ValueError, KeyError, TypeError) as e:
        sys.stderr.write("bad spec %s: %r\n" % (argv[1], e))
        return 2
    return driver.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))

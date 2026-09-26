#!/usr/bin/env python3
"""Run one run from a spec: python3 edr_driver.py <spec.json>.

Section 5 of docs/design.md. Python 3.6, standard library only, no import
of the package.
"""
import json
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
import traceback

POLL_S = 5
TICK_S = 0.5
TAIL_LINES = 80
GB = 1024.0 ** 3
LIC_RE = (r"Users of %s:\s*\(Total of (\d+) licen[cs]es? issued;"
          r"\s*Total of (\d+) licen[cs]es? in use\)")


class Fail(Exception):
    """A terminal phase: args are (exit code, phase)."""


class Killed(Exception):
    """A signal or a stop file ended the run."""


def free_gb(path):
    # type: (str) -> float
    try:
        return round(shutil.disk_usage(path).free / GB, 3)
    except OSError:
        return None


def du_gb(path):
    # type: (str) -> float
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
        self.root = spec["root"]
        self.state_file = spec["state_file"]
        self.queue_dir = spec["queue_dir"]
        self.stages = spec["stages"]
        self.shell = spec.get("shell") or "/bin/bash"
        self.limits = spec.get("limits") or {}
        self.env = dict(os.environ)
        # A value such as "/usr/sepp/bin:$PATH" names the host's own variables, so it
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
        self.killed = None
        self.stop_mode = None
        self.over_budget = None
        self.sigs = []
        self.stop_evt = threading.Event()
        now = int(time.time())
        self.hb = {
            "schema": 1, "run_id": self.run_id, "batch": spec.get("batch"),
            "label": spec.get("label"), "config": spec.get("config"),
            "host": spec.get("host"), "root": self.root, "driver_pid": os.getpid(),
            "pgids": [], "phase": "setup", "stage": None, "step": None, "step_name": None,
            "tasks": {}, "stages": {}, "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 0, "queued": 0},
            "started": now, "updated": now, "elapsed_s": 0, "disk_free_gb": None,
            "tree_gb": None, "exit": None, "killed_by": None, "last_cmd": None,
            "last_log": None, "log": None}

    # heartbeat

    def beat(self, tree=False):
        # type: (bool) -> None
        """Write the heartbeat by an atomic rename; `tree` also runs du."""
        free = free_gb(self.root)
        tree = du_gb(self.root) if tree else None
        with self.lock:
            hb = self.hb
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

    def beat_loop(self):
        period = float(self.limits.get("heartbeat_s") or 60)
        n = 0
        while not self.stop_evt.wait(period):
            self.beat(tree=n % 10 == 0)
            n += 1

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
        for pgid in list(self.procs):
            self.killpg(pgid, sig)

    @staticmethod
    def killpg(pgid, sig):
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass

    def read_stop(self):
        # type: () -> str
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
        # type: (str, str, int) -> tuple
        """Run a probe; return (rc, output), or (None, "") when it fails."""
        try:
            r = subprocess.run([self.shell, "-c", cmd], cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=timeout)
            return r.returncode, r.stdout
        except (OSError, subprocess.SubprocessError):
            return None, ""

    def reap(self, timeout):
        end = time.time() + timeout
        for pgid, p in list(self.procs.items()):
            try:
                p.wait(max(0.1, end - time.time()))
            except subprocess.TimeoutExpired:
                self.killpg(pgid, signal.SIGKILL)
                p.wait()
            self.forget(p)

    # limits

    def probe(self, lic):
        # type: (dict) -> int
        """Return the free seats of a licence, or None when the probe fails."""
        rc, out = self.sh(lic["probe"], self.root)
        m = re.search(LIC_RE % re.escape(lic["feature"]), out) if rc == 0 else None
        with self.lock:
            self.hb["licence_unknown"] = m is None
        if not m:
            sys.stderr.write("licence probe failed for %s: rc=%s\n" % (lic["feature"], rc))
            return None
        return int(m.group(1)) - int(m.group(2))

    def blocked(self, lic):
        # type: (dict) -> bool
        free = self.probe(lic)
        return free is not None and free - int(lic.get("seats_per_task") or 1) < int(lic.get("floor") or 0)

    def gate(self, st):
        lic = st.get("licence")
        if not lic:
            return
        self.set_phase("gate:" + st["name"], stage=st["name"])
        t0 = time.time()
        gate_max = float(self.limits.get("gate_max_s") or 0)
        while self.blocked(lic):
            left = gate_max - (time.time() - t0)
            if left <= 0:
                raise Fail(4, "FAILED:" + st["name"])
            self.wait(min(POLL_S, left))

    def host_full(self):
        # type: () -> bool
        free = free_gb(self.root)
        full = free is not None and free < float(self.limits.get("host_free_min_gb") or 0)
        with self.lock:
            self.hb["host_full"] = full
        return full

    def budget_over(self, budget, started):
        # type: (dict, int) -> str
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
            self.hb["step"] = step
            self.hb["step_name"] = steps[min(step, len(steps) - 1)] if steps else None

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
        for t in tasks:
            if os.path.exists(os.path.join(q, "done", t["id"])):
                continue
            try:
                os.close(os.open(os.path.join(q, "pending", t["id"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            except FileExistsError:
                pass
        parallel = max(1, int(st.get("parallel") or 1))
        lic, budget = st.get("licence"), st.get("budget") or {}
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
                if lic and self.blocked(lic):
                    gate_until = time.time() + POLL_S
                    break
                try:
                    os.rename(os.path.join(q, "pending", tid), os.path.join(q, "claimed", tid + "." + self.run_id))
                except OSError:
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
            raise Fail(2, "FAILED:setup")
        todo = self.stages[names.index(start):]
        need = float((todo[0].get("needs") or {}).get("disk_gb") or 0)
        free = free_gb(self.root)
        if free is not None and free < need:
            raise Fail(3, "FAILED:" + todo[0]["name"])
        for i, st in enumerate(todo):
            while self.host_full():
                self.wait(POLL_S)
            self.gate(st)
            if "tasks" in st:
                self.record_stage(st["name"], "running", attempt=1, started=int(time.time()), ended=None, exit=None)
                self.run_group(st)
                self.record_stage(st["name"], "done", ended=int(time.time()), exit=0)
            else:
                self.run_stage(st, start_at.get("checkpoint") if i == 0 else None)
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

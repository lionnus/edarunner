"""Render a driver spec for the demo flow into a tmp root, and drive the driver."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "local-demo"
DRIVER = REPO / "src" / "edarunner" / "driver" / "edr_driver.py"
# The interpreter of the driver under test: 3.6 when the machine has one, else the system python3.
PY36 = os.environ.get("EDR_DRIVER_PYTHON") or shutil.which("python3.6") or "/usr/bin/python3"
RUN_ID = "20260926_1200_a_demo_gHEAD"
STAGES = ("synth", "pnr", "export", "power")
STEPS = ["setup", "analyze", "elaborate", "synth", "cts", "route", "export"]
PROGRESS = "ls reports 2>/dev/null | grep -cE '^[0-9]+$'"
TOOL = {"name": "demo", "seats": 1, "probe": ["bash", "{root}/flow/seats.sh"]}
TASKS = {
    "k_small": {"kernel": "gemm", "test": "GEMM_M64_N64", "args": "M=64 N=64"},
    "k_big": {"kernel": "softmax", "test": "SOFTMAX_N512", "args": "N=512", "budget": {"hours": 2}},
    "k_bad": {"kernel": "bad", "test": "BAD", "args": ""},
}
LIMITS = {"host_free_min_gb": 0.1, "streak": 2, "heartbeat_s": 1, "gate_max_s": 30}


def render(text: str, ph: dict[str, str]) -> str:
    """Fill the placeholders in ph; leave every other brace as it is."""
    for k, v in ph.items():
        text = text.replace("{" + k + "}", v)
    return text


def render_spec(tmp_path: Path, stages=STAGES, tasks=("k_small", "k_big"), config="demo",
                overrides="", parallel=2, limits=None, env=None, start_at=None,
                budgets=None) -> dict:
    """Return a driver spec for the demo flow with root and state under tmp_path."""
    root = tmp_path / "edr" / "demo" / RUN_ID
    state = tmp_path / "state" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    if not (root / "flow").exists():
        shutil.copytree(DEMO / "flow", root / "flow")
    ph = {"root": str(root), "run_id": RUN_ID, "config": config, "overrides": overrides}
    tool = dict(TOOL, probe=[render(a, ph) for a in TOOL["probe"]], leases=str(tmp_path / ".edr" / "leases" / "demo"))
    flow = "bash {root}/flow/flow.sh"
    defs = {
        "synth": {
            "name": "synth", "cwd": str(root), "steps": STEPS[:4], "progress": PROGRESS,
            "cmd": render(flow + " synth {run_id} {config} LAST_STAGE=synth {overrides}", ph),
            "resume": render(flow + " synth {run_id} {config} FIRST_STAGE={checkpoint} LAST_STAGE=synth {overrides}", ph),
            "needs": {"cores": 1, "disk_gb": 0.1}, "tools": [tool],
            "budget": {"hours": 1, "disk_gb": 1, "kill": False},
            "retry": {"match": "licen[cs]e", "wait_s": 1, "max": 2}},
        "pnr": {
            "name": "pnr", "cwd": str(root), "steps": STEPS[:6], "progress": PROGRESS,
            "cmd": render(flow + " pnr {run_id} {config} {overrides}", ph),
            "resume": render(flow + " pnr {run_id} {config} FIRST_STAGE={checkpoint} {overrides}", ph),
            "needs": {"cores": 1, "disk_gb": 0.1}, "budget": {"hours": 1}},
        "export": {
            "name": "export", "cwd": str(root),
            "cmd": render(flow + " export {run_id} {config}", ph),
            "needs": {"cores": 1, "disk_gb": 0.1}},
        "power": {
            "name": "power", "cwd": str(root), "parallel": parallel,
            "after_each": "rm -f {task_dir}/wave.vcd",
            "needs": {"cores": 1, "disk_gb": 0.05}, "tools": [tool],
            "budget": {"hours": 1, "per": "task"},
            "tasks": [task_spec(t, ph) for t in tasks]},
    }
    for name, budget in (budgets or {}).items():
        defs[name]["budget"] = budget
    return {
        "schema": 1, "run_id": RUN_ID, "batch": "demo", "project": "demo", "label": "a",
        "config": config, "host": "local", "root": str(root),
        "state_file": str(state / (RUN_ID + ".json")),
        "queue_dir": str(state / (RUN_ID + ".queue")),
        "shell": "/bin/bash", "env": dict(env or {}),
        "limits": dict(LIMITS, **(limits or {})),
        "start_at": start_at or {"stage": stages[0], "checkpoint": None},
        "stages": [defs[s] for s in stages],
    }


def task_spec(tid: str, ph: dict[str, str]) -> dict:
    t = TASKS[tid]
    cmd = "DEMO_CONFIG={config} bash {root}/flow/kernel.sh %s %s %s" % (t["kernel"], t["test"], t["args"])
    spec = {"id": tid, "cmd": render(cmd, ph),
            "dir": render("{root}/simulation/tests/{config}/" + t["test"], ph)}
    if "budget" in t:
        spec["budget"] = t["budget"]
    return spec


def write_spec(spec: dict) -> Path:
    """Write the spec next to its state file and return its path."""
    path = Path(spec["state_file"]).with_suffix(".spec.json")
    path.write_text(json.dumps(spec, indent=1))
    return path


def start(spec: dict) -> subprocess.Popen:
    """Start the driver on the spec with PY36; its own log sits next to the spec."""
    spec_path = write_spec(spec)
    log = open(spec_path.with_suffix("").with_suffix(".driver.log"), "ab")
    return subprocess.Popen([PY36, str(DRIVER), str(spec_path)], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT)


def heartbeat(spec: dict) -> dict | None:
    try:
        return json.loads(Path(spec["state_file"]).read_text())
    except (OSError, ValueError):
        return None


def wait_for(spec: dict, pred, timeout: float = 30) -> dict:
    """Poll the heartbeat until pred(hb) holds; fail on the timeout."""
    end = time.time() + timeout
    while time.time() < end:
        hb = heartbeat(spec)
        if hb and pred(hb):
            return hb
        time.sleep(0.05)
    raise AssertionError("heartbeat never matched: %r" % (heartbeat(spec),))


def finish(proc: subprocess.Popen, spec: dict, timeout: float = 60) -> tuple[int, dict]:
    """Wait for the driver to exit; return (exit code, final heartbeat)."""
    return proc.wait(timeout=timeout), heartbeat(spec)

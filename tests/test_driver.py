"""edr_driver.py under /usr/bin/python3 against the demo flow."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from helpers_driver import DRIVER, PY36, RUN_ID, finish, heartbeat, render_spec, start, wait_for


def test_compiles_on_py36() -> None:
    subprocess.run([PY36, "-m", "py_compile", str(DRIVER)], check=True)


def test_synth_only_reaches_done(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",))
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (0, "done", 0)
    root = Path(spec["root"])
    for n in range(4):
        assert (root / "reports" / str(n) / "area.rpt").exists()
        assert (root / "reports" / str(n) / "qor.rpt").exists()
    assert hb["step"] == 4 and hb["stage"] == "synth" and hb["pgids"] == []
    assert hb["last_cmd"].startswith("bash ") and "synth done" in hb["last_log"]
    assert (root / "log" / "synth.log").exists()


def test_group_runs_parallel_and_counts_a_failure(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("power",), tasks=("k_small", "k_big", "k_bad"), parallel=2)
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (8, "INCOMPLETE:1f0s", 8)
    small, big, bad = (hb["tasks"][t] for t in ("k_small", "k_big", "k_bad"))
    assert small["phase"] == big["phase"] == "done"
    assert small["started"] < big["ended"] and big["started"] < small["ended"]
    assert bad["phase"] == "failed" and bad["exit"] == 1
    assert bad["signature"] == "boom: kernel bad failed"
    assert hb["counts"] == {"done": 2, "failed": 1, "skipped": 0, "running": 0, "queued": 0}
    q = Path(spec["queue_dir"]) / "power"
    assert sorted(os.listdir(q / "done")) == ["k_bad", "k_big", "k_small"]
    assert os.listdir(q / "pending") == [] and os.listdir(q / "claimed") == []
    root = Path(spec["root"])
    assert (root / "simulation/tests/demo/GEMM_M64_N64/power/reports/power.csv").exists()
    assert not (root / "simulation/tests/demo/GEMM_M64_N64/wave.vcd").exists()
    assert (root / "log" / "power.k_bad.log").exists()


def test_licence_failure_retries_once(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), config="fail_licence")
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"]) == (0, "done")
    log = (Path(spec["root"]) / "log" / "synth.log").read_text()
    assert "license checkout failed" in log
    assert log.count("step 0 setup") == 2
    assert (Path(spec["root"]) / "reports" / "3" / "qor.rpt").exists()


def test_sigterm_kills_the_group_and_reports(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",))
    proc = start(spec)
    hb = wait_for(spec, lambda h: h["phase"] == "stage:synth" and h["pgids"])
    pgid = hb["pgids"][0]
    proc.send_signal(signal.SIGTERM)
    rc, hb = finish(proc, spec)
    assert (rc, hb["phase"], hb["killed_by"], hb["exit"]) == (10, "KILLED:SIGTERM", "SIGTERM", 10)
    assert hb["pgids"] == []
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("process group %d still alive" % pgid)
    assert not (Path(spec["root"]) / "reports" / "3").exists()


def test_stop_file_after_task_ends_the_group(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("power",), tasks=("k_small", "k_big"), parallel=1)
    proc = start(spec)
    wait_for(spec, lambda h: h["tasks"].get("k_small", {}).get("phase") == "running")
    stop = Path(spec["state_file"]).with_name(RUN_ID + ".stop")
    stop.write_text("after-task")
    rc, hb = finish(proc, spec)
    assert (rc, hb["phase"], hb["exit"]) == (10, "STOPPED", 10)
    assert hb["tasks"]["k_small"]["phase"] == "done" and "k_big" not in hb["tasks"]
    assert (Path(spec["queue_dir"]) / "power" / "pending" / "k_big").exists()
    assert hb["counts"]["done"] == 1


def test_stop_file_now_kills_the_task(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("power",), tasks=("k_small",), env={"DEMO_SLEEP": "20"})
    proc = start(spec)
    hb = wait_for(spec, lambda h: h["tasks"].get("k_small", {}).get("phase") == "running")
    pgid = hb["tasks"]["k_small"]["pgid"]
    Path(spec["state_file"]).with_name(RUN_ID + ".stop").write_text("now")
    rc, hb = finish(proc, spec, timeout=15)
    assert (rc, hb["phase"], hb["killed_by"]) == (10, "STOPPED", "stop")
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_budget_marks_over_budget_without_a_kill(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), budgets={"synth": {"hours": 0.0003, "kill": False}})
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (9, "OVER_BUDGET:synth", 9)
    assert (Path(spec["root"]) / "reports" / "3" / "qor.rpt").exists()
    assert hb["killed_by"] is None


def test_keep_file_extends_the_budget(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), budgets={"synth": {"hours": 0.0003, "kill": False}})
    proc = start(spec)
    wait_for(spec, lambda h: h["phase"] == "stage:synth")
    keep = Path(spec["state_file"]).with_name(RUN_ID + ".keep.json")
    keep.write_text(json.dumps({"hours": 1, "ack": True}))
    rc, hb = finish(proc, spec)
    assert (rc, hb["phase"]) == (0, "done")
    assert hb["keep_hours"] == 1


def test_gate_waits_then_fails(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), env={"DEMO_LIC_USED": "9"}, limits={"gate_max_s": 3})
    proc = start(spec)
    t0 = time.time()
    wait_for(spec, lambda h: h["phase"] == "gate:synth")
    rc, hb = finish(proc, spec)
    assert time.time() - t0 >= 3
    assert (rc, hb["phase"], hb["exit"]) == (4, "FAILED:synth", 4)
    assert not (Path(spec["root"]) / "log" / "synth.log").exists()


def test_checkpoint_without_resume_fails_before_the_command(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("export",), start_at={"stage": "export", "checkpoint": "cts"})
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (2, "FAILED:export", 2)
    assert hb["last_cmd"] is None and not (Path(spec["root"]) / "log" / "export.log").exists()


def test_bad_spec_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.spec.json"
    bad.write_text("{}")
    assert subprocess.run([PY36, str(DRIVER), str(bad)], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 2
    assert heartbeat({"state_file": str(tmp_path / "none.json")}) is None


def test_spec_env_expands_host_variables(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), env={"PATH": "/edr-nowhere:$PATH", "EDR_HOME_COPY": "${HOME}/x"})
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"]) == (0, "done"), hb
    log = (Path(spec["root"]) / "log" / "synth.log").read_text()
    assert "command not found" not in log

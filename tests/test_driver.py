"""edr_driver.py under helpers_driver.PY36 against the demo flow."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from helpers_driver import DRIVER, PY36, RUN_ID, finish, heartbeat, render_spec, start, wait_for


def test_compiles_on_py36() -> None:
    probe = [PY36, "-c", "import sys; print(sys.version_info[:2] == (3, 6))"]
    try:
        is36 = subprocess.run(probe, stdout=subprocess.PIPE, text=True).stdout.strip() == "True"
    except OSError:
        is36 = False
    if not is36:
        pytest.skip(f"{PY36} is not Python 3.6; set EDR_DRIVER_PYTHON")
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
    times = hb["step_times"]["synth"]
    assert {"cpu_pct", "rss_gb", "tree_gb"} <= set(hb)
    assert "4" in times and sorted(times.values()) == [times[k] for k in sorted(times, key=int)]
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
    assert os.listdir(spec["stages"][0]["tools"][0]["leases"]) == []
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
    assert hb["pgids"] == [] and os.listdir(spec["stages"][0]["tools"][0]["leases"]) == []
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
    spec = render_spec(tmp_path, stages=("synth",), env={"DEMO_SEATS_USED": "10"}, limits={"gate_max_s": 3})
    proc = start(spec)
    t0 = time.time()
    hb = wait_for(spec, lambda h: h["phase"] == "gate:synth" and h.get("gate"))
    assert hb["gate"] == "demo: 0 free, 0 held by others, 1 needed"
    rc, hb = finish(proc, spec)
    assert time.time() - t0 >= 3
    assert (rc, hb["phase"], hb["exit"]) == (4, "FAILED:synth", 4)
    assert not (Path(spec["root"]) / "log" / "synth.log").exists()


def test_gate_waits_then_passes(tmp_path: Path) -> None:
    seats = tmp_path / "seats"
    seats.write_text("1 4\n")
    spec = render_spec(tmp_path, stages=("synth",))
    spec["stages"][0]["tools"] = [{"name": "sim", "seats": 2, "probe": ["cat", str(seats)]}]
    proc = start(spec)
    hb = wait_for(spec, lambda h: h.get("gate"))
    assert hb["phase"] == "gate:synth" and hb["gate"] == "sim: 1 free, 0 held by others, 2 needed" and hb["last_cmd"] is None
    seats.write_text("2 4\n")
    rc, hb = finish(proc, spec)
    assert (rc, hb["phase"], hb["gate"]) == (0, "done", None)
    log = Path(spec["state_file"]).with_suffix("").with_suffix(".driver.log").read_text()
    assert log.splitlines() == ["gate synth: wait for sim: 1 free, 0 held by others, 2 needed", "gate synth: open"]


def test_gate_lets_a_failed_probe_through(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",))
    spec["stages"][0]["tools"] = [{"name": "sim", "seats": 1, "probe": ["false"]}]
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"]) == (0, "done") and "gate" not in hb
    log = Path(spec["state_file"]).with_suffix("").with_suffix(".driver.log").read_text()
    assert log.startswith("tool probe failed for sim: rc=1")


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


def test_streak_of_equal_signatures_sets_looping(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("power",), tasks=("k_bad", "k_small"), parallel=1)
    tasks = spec["stages"][0]["tasks"]
    tasks.insert(1, dict(tasks[0], id="k_bad2"))
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["looping"]) == (8, "INCOMPLETE:2f0s", True)
    assert hb["tasks"]["k_bad"]["signature"] == hb["tasks"]["k_bad2"]["signature"] == "boom: kernel bad failed"
    assert "k_small" not in hb["tasks"] and hb["counts"]["failed"] == 2
    assert (Path(spec["queue_dir"]) / "power" / "pending" / "k_small").exists()


def test_host_full_starts_nothing(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), limits={"host_free_min_gb": 1e9})
    proc = start(spec)
    hb = wait_for(spec, lambda h: h.get("host_full") is True)
    assert hb["phase"] == "setup" and hb["last_cmd"] is None
    Path(spec["state_file"]).with_name(RUN_ID + ".stop").write_text("now")
    rc, hb = finish(proc, spec, timeout=15)
    assert (rc, hb["phase"], hb["killed_by"], hb["host_full"]) == (10, "STOPPED", "stop", True)
    assert hb["stages"] == {} and not (Path(spec["root"]) / "log" / "synth.log").exists()


def test_needs_disk_gb_refuses_a_stage_and_skips_a_task(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",))
    spec["stages"][0]["needs"]["disk_gb"] = 1e9
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (3, "FAILED:synth", 3)
    assert hb["last_cmd"] is None and not (Path(spec["root"]) / "log" / "synth.log").exists()

    spec = render_spec(tmp_path / "group", stages=("power",), tasks=("k_small", "k_big"))
    spec["stages"][0]["tasks"][0]["needs"] = {"disk_gb": 1e9}
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (8, "INCOMPLETE:0f1s", 8)
    assert hb["tasks"]["k_small"]["phase"] == "skipped" and hb["tasks"]["k_big"]["phase"] == "done"
    assert hb["counts"] == {"done": 1, "failed": 0, "skipped": 1, "running": 0, "queued": 0}
    assert (Path(spec["queue_dir"]) / "power" / "pending" / "k_small").exists()


def test_budget_kill_ends_the_stage(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth",), budgets={"synth": {"hours": 0.0003, "kill": True}})
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"], hb["over_budget"]) == (9, "OVER_BUDGET:synth", 9, "synth")
    assert hb["stages"]["synth"]["status"] == "over_budget" and hb["stages"]["synth"]["exit"] != 0
    assert hb["killed_by"] is None and hb["pgids"] == []
    assert not (Path(spec["root"]) / "reports" / "3").exists()


def test_per_task_budget_kills_that_task_only(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("power",), tasks=("k_small", "k_big"), parallel=2,
                       env={"DEMO_SLEEP": "4"}, budgets={"power": {"hours": 0.0003, "per": "task"}})
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (8, "INCOMPLETE:1f0s", 8)
    small, big = hb["tasks"]["k_small"], hb["tasks"]["k_big"]
    assert small["phase"] == "failed" and small["over_budget"] is True and small["exit"] != 0
    assert big["phase"] == "done" and "over_budget" not in big  # its own 2 h budget holds
    assert "over_budget" not in hb and hb["killed_by"] is None
    assert sorted(os.listdir(Path(spec["queue_dir"]) / "power" / "done")) == ["k_big", "k_small"]


def test_checkpoint_resumes_the_first_stage_only(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth", "pnr"), start_at={"stage": "synth", "checkpoint": "elaborate"})
    rc, hb = finish(start(spec), spec)
    assert (rc, hb["phase"], hb["exit"]) == (0, "done", 0)
    root = Path(spec["root"])
    assert [n for n in range(6) if (root / "reports" / str(n) / "qor.rpt").exists()] == [2, 3, 4, 5]
    synth, pnr = (root / "log" / "synth.log").read_text(), (root / "log" / "pnr.log").read_text()
    assert "FIRST_STAGE=elaborate" in synth.splitlines()[0] and "step 0 setup" not in synth
    assert "FIRST_STAGE" not in pnr and "step 4 cts" in pnr
    assert hb["stages"]["synth"]["status"] == hb["stages"]["pnr"]["status"] == "done"


def test_two_shards_claim_from_one_queue(tmp_path: Path) -> None:
    a = render_spec(tmp_path, stages=("power",), tasks=("k_small", "k_big"), parallel=1, env={"DEMO_SLEEP": "3"})
    b = dict(a, run_id=RUN_ID + "_b", state_file=str(Path(a["state_file"]).with_name(RUN_ID + "_b.json")))
    pa = start(a)
    wait_for(a, lambda h: h["tasks"].get("k_small", {}).get("phase") == "running")
    pb = start(b)
    (rc_a, ha), (rc_b, hb) = finish(pa, a), finish(pb, b)
    assert (rc_a, ha["phase"], rc_b, hb["phase"]) == (0, "done", 0, "done")
    q = Path(a["queue_dir"]) / "power"
    assert sorted(os.listdir(q / "done")) == ["k_big", "k_small"] and os.listdir(q / "claimed") == []
    assert (set(ha["tasks"]), set(hb["tasks"])) == ({"k_small"}, {"k_big"})


def test_sighup_kills_the_group_and_reports(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("power",), tasks=("k_small",), env={"DEMO_SLEEP": "20"})
    proc = start(spec)
    hb = wait_for(spec, lambda h: h["tasks"].get("k_small", {}).get("phase") == "running")
    pgid = hb["tasks"]["k_small"]["pgid"]
    proc.send_signal(signal.SIGHUP)
    rc, hb = finish(proc, spec, timeout=15)
    assert (rc, hb["phase"], hb["killed_by"], hb["exit"]) == (10, "KILLED:SIGHUP", "SIGHUP", 10)
    assert hb["pgids"] == []
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("process group %d still alive" % pgid)
    assert os.listdir(Path(spec["queue_dir"]) / "power" / "claimed") == ["k_small." + RUN_ID]


# seat leases

def _load_driver():
    spec = importlib.util.spec_from_file_location("edr_driver_under_test", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _two_drivers(tmp_path: Path, free: int):
    """Two drivers of two runs that share one probe value and one lease directory."""
    mod = _load_driver()
    seats = tmp_path / "seats"
    seats.write_text(f"{free} 10\n")
    tool = {"name": "sim", "seats": 1, "probe": ["cat", str(seats)], "leases": str(tmp_path / "state" / "leases" / "sim")}
    out = []
    for run_id in (RUN_ID, RUN_ID + "_b"):
        spec = render_spec(tmp_path, stages=("synth",))
        spec["run_id"] = run_id
        spec["state_file"] = str(Path(spec["state_file"]).with_name(run_id + ".json"))
        path = Path(spec["state_file"]).with_suffix(".spec.json")
        path.write_text(json.dumps(spec))
        d = mod.Driver(str(path))
        d.hb["stage"] = "synth"
        out.append(d)
    return out, [tool], Path(tool["leases"])


def test_second_driver_waits_for_a_seat_the_first_leased(tmp_path: Path) -> None:
    (a, b), tools, leases = _two_drivers(tmp_path, free=1)
    assert a.take(tools, a.run_id + ".synth", {"hours": 2}) is None
    lease = json.loads((leases / f"{RUN_ID}.synth.0").read_text())
    assert (lease["run_id"], lease["stage"], lease["pid"], lease["host"], lease["budget_s"]) == (
        RUN_ID, "synth", os.getpid(), "local", 7200.0)
    assert b.take(tools, b.run_id + ".synth", {}) == "sim: 1 free, 1 held by others, 1 needed"
    assert os.listdir(leases) == [f"{RUN_ID}.synth.0"]
    a.release()
    assert b.take(tools, b.run_id + ".synth", {}) is None and os.listdir(leases) == [f"{RUN_ID}_b.synth.0"]


def test_the_later_of_two_drivers_that_saw_one_seat_backs_off(tmp_path: Path) -> None:
    (a, b), tools, leases = _two_drivers(tmp_path, free=1)
    assert a.take(tools, a.run_id + ".synth", {}) is None
    real, calls = b.others, []

    def blind(tool, key, now):
        # b counted before a's rename landed.
        calls.append(key)
        return [] if len(calls) == 1 else real(tool, key, now)

    b.others = blind
    assert b.take(tools, b.run_id + ".synth", {}) == "sim: 1 free, 1 held by others, 1 needed"
    assert os.listdir(leases) == [f"{RUN_ID}.synth.0"] and b.leases == {}


def test_a_lease_older_than_lease_s_no_longer_counts(tmp_path: Path) -> None:
    (a, b), tools, leases = _two_drivers(tmp_path, free=1)
    a.take(tools, a.run_id + ".synth", {})
    path = leases / f"{RUN_ID}.synth.0"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), ts=time.time() - 601)))
    assert b.take(tools, b.run_id + ".synth", {}) is None


def test_leases_vanish_at_stage_end(tmp_path: Path) -> None:
    spec = render_spec(tmp_path, stages=("synth", "export"))
    leases = Path(spec["stages"][0]["tools"][0]["leases"])
    proc = start(spec)
    wait_for(spec, lambda h: h["phase"] == "stage:synth")
    assert os.listdir(leases) == [f"{RUN_ID}.synth.0"]
    wait_for(spec, lambda h: h["phase"] == "stage:export")
    assert os.listdir(leases) == []
    assert finish(proc, spec)[0] == 0



def test_heartbeat_names_the_host_the_job_and_the_samples(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDR_SCHED_ID", "42.0")
    spec = render_spec(tmp_path, stages=("synth",))
    spec["host"] = None
    proc = start(spec)
    hb = wait_for(spec, lambda h: h["phase"] == "stage:synth" and h["pgids"] and h["log_bytes"])
    assert hb["cpu_s"] >= 0 and hb["log"].endswith("synth.log")
    rc, hb = finish(proc, spec)
    assert (rc, hb["host"], hb["sched_id"]) == (0, socket.gethostname(), "42.0")
    assert hb["log_bytes"] == (Path(spec["root"]) / "log" / "synth.log").stat().st_size and hb["cpu_s"] == 0.0


def test_cpu_seconds_of_a_busy_group_from_proc_and_from_ps(monkeypatch) -> None:
    mod = _load_driver()
    busy = subprocess.Popen(["python3", "-c", "import time\nend = time.time() + 30\nwhile time.time() < end: pass"],
                            start_new_session=True)
    try:
        time.sleep(1.5)
        assert mod.cpu_seconds([busy.pid]) > 0.3 and mod.cpu_seconds([]) == 0.0
        monkeypatch.setattr(mod.os.path, "isdir", lambda p: False)
        assert mod.cpu_seconds([busy.pid]) >= 1.0
    finally:
        busy.kill()
        busy.wait()

"""launch.py and sync.py against examples/local-demo on the host `local`; every write goes to tmp_path."""

from __future__ import annotations

import getpass
import json
import shutil
import time
from pathlib import Path

import pytest

from edarunner import config, launch, sync
from edarunner.guards import Refuse
from edarunner.hosts import HostProbe, Ssh
from edarunner.ledger import Ledger

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
DATE = "20260926_1200"
SPEC_KEYS = {"schema", "run_id", "batch", "project", "label", "config", "host", "root", "state_file",
             "queue_dir", "shell", "env", "limits", "netlist_stage", "start_at", "stages"}


class FakeProbeSsh(Ssh):
    """The real local ssh wrapper with a probe that points the scratch into tmp_path."""

    def __init__(self, site, mount: Path) -> None:
        super().__init__(site)
        self.mount = mount

    def probe(self, host: str) -> HostProbe:
        return HostProbe(host, 4.0, 8.0, str(self.mount), 50.0)


@pytest.fixture
def env(tmp_path: Path):
    project = config.load_project(DEMO)
    project.state = tmp_path / "state"
    project.limits.heartbeat_s = 1
    batch = config.load_batch(project, "demo")
    ssh = FakeProbeSsh(project.site, tmp_path / "scratch")
    with Ledger(tmp_path / "edr.db") as ledger:
        yield project, batch, ssh, ledger


def strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


def synth_only(batch) -> None:
    batch.jobs = [batch.jobs[0]]
    batch.jobs[0].stages, batch.jobs[0].tasks = ["synth"], []


def src_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    shutil.copytree(DEMO / "flow", src / "flow")
    return src


def wait_hb(path: Path, pred, timeout: float = 60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            hb = json.loads(path.read_text())
            if pred(hb):
                return hb
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"heartbeat never matched: {path}")


# plan


def test_plan_renders_the_demo_spec(env, tmp_path: Path) -> None:
    project, batch, ssh, ledger = env
    plans = launch.plan(project, batch, ssh, ledger, date=DATE)
    a, b = plans
    assert [p.problems for p in plans] == [[], []] and not a.queued
    assert a.run_id == f"{DATE}_a_demo_gHEAD" and b.run_id == f"{DATE}_b_nodw_demo_DW0_gHEAD"
    root = f"{tmp_path}/scratch/{getpass.getuser()}/edr/demo/{a.run_id}"
    assert a.host == "local" and a.root == root
    spec = a.spec
    assert set(spec) == SPEC_KEYS and spec["schema"] == 1 and spec["shell"] == "/bin/bash"
    assert spec["state_file"] == f"{tmp_path}/state/demo/{a.run_id}.json"
    assert spec["queue_dir"] == f"{tmp_path}/state/demo/{a.run_id}.queue"
    assert spec["limits"] == {"host_free_min_gb": 1, "streak": 2, "heartbeat_s": 1, "gate_max_s": 30}
    assert spec["start_at"] == {"stage": "synth", "checkpoint": None} and spec["netlist_stage"] == 11
    synth, pnr, export, power = spec["stages"]
    assert [s["name"] for s in spec["stages"]] == ["synth", "pnr", "export", "power"]
    assert synth["cmd"].split() == ["bash", f"{root}/flow/flow.sh", "synth", a.run_id, "demo", "LAST_STAGE=synth"]
    assert "FIRST_STAGE={checkpoint}" in synth["resume"] and synth["cwd"] == root
    assert synth["needs"] == {"cores": 1, "disk_gb": 0.1} and synth["steps"] == ["setup", "analyze", "elaborate", "synth"]
    assert synth["licence"] == {"name": "demo", "feature": "demo", "floor": 2, "seats_per_task": 1,
                                "probe": f"bash {root}/flow/lmstat.sh"}
    assert synth["budget"] == {"hours": 1, "disk_gb": 1} and synth["retry"] == {"match": "licen[cs]e", "wait_s": 1, "max": 2}
    assert "retry" not in pnr and "licence" not in export and "cmd" not in power
    assert power["parallel"] == 2 and power["after_each"] == "rm -f {task_dir}/wave.vcd"
    assert power["licence"]["seats_per_task"] == 1 and power["budget"] == {"hours": 1, "per": "task"}
    small, big = power["tasks"]
    assert small["dir"] == f"{root}/simulation/tests/demo/GEMM_M64_N64"
    assert small["cmd"] == f"DEMO_CONFIG=demo bash {root}/flow/kernel.sh gemm GEMM_M64_N64 M=64 N=64"
    assert small["needs"] == {"cores": 1, "disk_gb": 0.05} and small["budget"] == {"hours": 1, "per": "task"}
    assert big["budget"] == {"hours": 2, "per": "task"}
    left = [s for s in strings(spec) if "{" in s.replace("{checkpoint}", "").replace("{task_dir}", "")]
    assert left == []
    assert " DW=0" in b.spec["stages"][0]["cmd"] and b.spec["stages"][0]["cmd"].endswith("DW=0")


def test_plan_reports_problems(env) -> None:
    project, batch, ssh, ledger = env
    batch.jobs[1].overrides = {"bad key": "1"}
    project.stages["export"].cmd += " {nope}"
    a, b = launch.plan(project, batch, ssh, ledger, date=DATE)
    assert any("nope" in p for p in a.problems) and a.spec == {} and not a.queued
    assert any("'bad key'" in p for p in b.problems)
    batch.jobs[1].overrides = {"DW": "0"}
    batch.jobs[1].stages = ["export"]
    project.stages["export"].cmd = "true"
    _, b = launch.plan(project, batch, ssh, ledger, date=DATE)
    assert b.problems == ["overrides given, but no stage of the job uses {overrides}"]


def test_plan_reuses_a_ledger_run(env) -> None:
    project, batch, ssh, ledger = env
    ledger.upsert_run({"run_id": f"{DATE}_a_demo_gOLD", "batch": "old", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD"})
    batch.jobs[0].reuse = {"label": "a", "latest": True}
    batch.jobs[0].stages = ["pnr"]
    a = launch.plan(project, batch, ssh, ledger, date=DATE)[0]
    assert a.problems == [] and a.reuse == f"{DATE}_a_demo_gOLD" and a.src == "OLD"
    assert a.root == "/x/edr/old" and a.run_id == f"{DATE}_a_demo_gOLD"
    assert a.spec["start_at"]["stage"] == "pnr" and a.spec["stages"][0]["cwd"] == "/x/edr/old"


# launch and stop


def test_launch_local_runs_synth_to_done(env, tmp_path: Path) -> None:
    project, batch, ssh, ledger = env
    synth_only(batch)
    out = launch.launch(project, batch, ssh, ledger, src_dir=src_tree(tmp_path))
    (row,) = out
    assert row["started"] and row["pid"] and row["problems"] == []
    run_id = row["run_id"]
    state = tmp_path / "state" / "demo"
    assert (state / "RUN_DATE").read_text().strip() == run_id[:13]
    assert (tmp_path / "state" / "bin" / "demo" / "edr_driver.py").exists()
    spec = json.loads((state / f"{run_id}.spec.json").read_text())
    assert [s["name"] for s in spec["stages"]] == ["synth"]
    assert (Path(row["root"]) / "flow" / "flow.sh").exists()
    assert ledger.run(run_id)["state"] == "running" and ledger.batches()[0]["batch"] == "demo"
    assert [e["kind"] for e in ledger.events(run_id=run_id)] == ["launch"]
    hb = wait_hb(state / f"{run_id}.json", lambda h: h["phase"] == "done")
    assert hb["exit"] == 0 and (Path(row["root"]) / "reports" / "3" / "qor.rpt").exists()
    again = launch.launch(project, batch, ssh, ledger, src_dir=src_tree(tmp_path / "again"))
    assert not again[0]["started"] and "already launched" in again[0]["problems"][0]


def test_stop_kills_a_running_driver(env, tmp_path: Path) -> None:
    project, batch, ssh, ledger = env
    synth_only(batch)
    (row,) = launch.launch(project, batch, ssh, ledger, src_dir=src_tree(tmp_path))
    hb_path = tmp_path / "state" / "demo" / f"{row['run_id']}.json"
    hb = wait_hb(hb_path, lambda h: h["phase"] == "stage:synth")
    run_row = ledger.run(row["run_id"])
    assert launch.stop(ssh, ledger, run_row, hb, now=True, grace_s=15, why="test", state=project.state)
    hb = wait_hb(hb_path, lambda h: h["exit"] is not None, timeout=20)
    assert hb["phase"] in ("KILLED:SIGTERM", "STOPPED") and hb["exit"] == 10 and hb["pgids"] == []
    assert not ssh.pid_alive("local", hb["driver_pid"])
    assert [e["kind"] for e in ledger.events(run_id=row["run_id"])] == ["launch", "stop"]
    assert launch.stop(ssh, ledger, run_row, hb, after_task=True, why="later", state=project.state)
    assert (hb_path.with_name(f"{row['run_id']}.stop")).read_text().strip() == "after-task"


# dry run and guards


def listing(root: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def test_dry_run_writes_nothing(env, tmp_path: Path, capsys) -> None:
    project, batch, ssh, ledger = env
    state = project.state
    state.mkdir()
    (state / "demo").mkdir()
    (state / "demo" / "RUN_DATE").write_text(DATE + "\n")
    before = listing(state)
    out = launch.launch(project, batch, ssh, ledger, dry_run=True, src_dir=src_tree(tmp_path))
    assert listing(state) == before and not (tmp_path / "scratch").exists()
    assert ledger.runs() == [] and ledger.events() == [] and ledger.batches() == []
    assert [r["run_id"] for r in out] == [f"{DATE}_a_demo_gHEAD", f"{DATE}_b_nodw_demo_DW0_gHEAD"]
    assert all(not r["started"] and r["problems"] == [] for r in out)
    text = capsys.readouterr().out
    assert "rsync -a --delete --exclude=.git" in text and f"{DATE}_a_demo_gHEAD" in text
    assert launch.stop(ssh, ledger, {"run_id": "r", "host": "local", "batch": "demo"}, {"driver_pid": 1, "pgids": [2]},
                       dry_run=True) and ledger.events() == []


def test_launch_refuses_a_dirty_source(env) -> None:
    project, batch, ssh, ledger = env
    batch.source = "abc1234-dirty-deadbeef"
    with pytest.raises(Refuse):
        launch.launch(project, batch, ssh, ledger, dry_run=True)


def test_guard_normalizes_dotdot(tmp_path: Path) -> None:
    from edarunner.guards import assert_safe_target
    for bad in ("/a/edr/b/c/../../../..", f"{tmp_path}/edr/../../{tmp_path.name}", "/scratch2/u/edr/x/../../../.."):
        with pytest.raises(Refuse):
            assert_safe_target(bad, "/edr/", 4)
    assert assert_safe_target("/scratch2/u/edr/p/run/./", "/edr/", 4) == Path("/scratch2/u/edr/p/run")


def test_pin_date_treats_an_empty_pin_as_missing(tmp_path: Path) -> None:
    pin = tmp_path / "b" / "RUN_DATE"
    pin.parent.mkdir()
    pin.write_text("")
    assert launch.pin_date(tmp_path, "b", dry_run=True) and pin.read_text() == ""
    date = launch.pin_date(tmp_path, "b")
    assert pin.read_text() == date + "\n" and launch.pin_date(tmp_path, "b") == date
    assert sorted(p.name for p in pin.parent.iterdir()) == ["RUN_DATE"]


@pytest.mark.parametrize("pid", [0, 1, -1, "7", 4242.0, None])
def test_stop_refuses_a_bad_driver_pid(env, pid) -> None:
    project, batch, ssh, ledger = env
    with pytest.raises(Refuse):
        launch.stop(ssh, ledger, {"run_id": "r", "host": "local", "batch": "demo"}, {"driver_pid": pid, "pgids": []})
    assert ledger.events() == []


def test_sync_tree_guards_the_target(env, tmp_path: Path) -> None:
    project, batch, ssh, ledger = env
    with pytest.raises(Refuse):
        sync.sync_tree(ssh, "local", tmp_path, str(tmp_path / "no-marker"), [], "/edr/", 3)
    assert sync.sync_tree(ssh, "local", tmp_path, str(tmp_path / "a" / "edr" / "b"), [], "/edr/", 3, dry_run=True)
    assert not (tmp_path / "a").exists()


def test_publish_driver_copies_by_rename(tmp_path: Path) -> None:
    dest = sync.publish_driver(tmp_path, "b1", launch.DRIVER_SRC)
    assert dest == tmp_path / "bin" / "b1" / "edr_driver.py" and dest.read_bytes() == launch.DRIVER_SRC.read_bytes()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["edr_driver.py"]
    assert sync.publish_driver(tmp_path, "b2", launch.DRIVER_SRC, dry_run=True) == tmp_path / "bin" / "b2" / "edr_driver.py"
    assert not (tmp_path / "bin" / "b2").exists()


def test_reuse_renders_tree_id_of_the_old_run(env) -> None:
    project, batch, ssh, ledger = env
    ledger.upsert_run({"run_id": "20260101_0000_a_demo_gOLD", "batch": "old", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD"})
    batch.jobs[0].reuse = {"label": "a", "latest": True}
    batch.jobs[0].stages = ["pnr"]
    plans = launch.plan(project, batch, ssh, ledger, date=DATE)
    a = next(p for p in plans if p.label == "a")
    assert a.values["tree_id"] == "20260101_0000_a_demo_gOLD" and a.values["run_id"] != a.values["tree_id"]
    b = next(p for p in plans if p.label != "a")
    assert b.values["tree_id"] == b.values["run_id"]


def test_remote_driver_uses_the_login_python(tmp_path: Path) -> None:
    class FakeSsh:
        def run(self, host, cmd, timeout_s=None):
            self.cmd = cmd
            return 0, "4242\n", ""
    ssh = FakeSsh()
    pid = launch.start_driver(ssh, "larain7", tmp_path / "d.py", tmp_path / "s.json", tmp_path / "l.log",
                              {"PATH": "/usr/sepp/bin:$PATH"})
    assert pid == 4242
    assert ssh.cmd.startswith("py=$(command -v python3); setsid nohup \"$py\" ")
    assert "export" not in ssh.cmd and "/usr/sepp" not in ssh.cmd and "python3 " not in ssh.cmd.split("nohup")[1]


def test_project_env_is_rendered_over_the_site_env(env) -> None:
    project, batch, ssh, ledger = env
    project.env = {"PATH": "{root}/.venv/bin:$PATH", "FLOW_TAG": "{build_tag}"}
    a = launch.plan(project, batch, ssh, ledger, date=DATE)[0]
    e = a.spec["env"]
    assert e["PATH"] == f"{a.root}/.venv/bin:$PATH" and e["FLOW_TAG"] == a.values["build_tag"]
    for k, val in project.site.env.items():
        assert k in e


def test_tree_id_survives_a_chain_of_reuse(env) -> None:
    project, batch, ssh, ledger = env
    ledger.upsert_run({"run_id": "20260101_0000_a_demo_gOLD", "batch": "old", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD", "tree_id": "20260101_0000_a_demo_gOLD"})
    ledger.upsert_run({"run_id": "20260102_0000_a_demo_gOLD", "batch": "mid", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD", "tree_id": "20260101_0000_a_demo_gOLD"})
    batch.jobs[0].reuse = {"label": "a", "latest": True}
    batch.jobs[0].stages = ["pnr"]
    a = next(p for p in launch.plan(project, batch, ssh, ledger, date=DATE) if p.label == "a")
    assert a.reuse == "20260102_0000_a_demo_gOLD" and a.values["tree_id"] == "20260101_0000_a_demo_gOLD"
    assert launch._run_row(a, batch)["tree_id"] == "20260101_0000_a_demo_gOLD"

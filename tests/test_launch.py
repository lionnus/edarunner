"""launch.py and sync.py against examples/local-demo on the host `local`; every write goes to tmp_path."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import signal
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import edarunner
from edarunner import config, launch, sync
from edarunner.backend import Handle, Request, SshBackend
from edarunner.config import ConfigError
from edarunner.db import Database
from edarunner.guards import Refuse, assert_safe_target
from edarunner.hosts import HostProbe, Ssh
from edarunner.model import Needs
from helpers_driver import wait_for

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
DATE = "20260926_1200"
SPEC_KEYS = {"schema", "run_id", "batch", "project", "label", "config", "vars", "host", "root", "state_file",
             "queue_dir", "shell", "env", "limits", "start_at", "stages"}


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
    project.state_dir = tmp_path / "state"
    project.limits.heartbeat_s = 1
    batch = config.load_batch(project, "demo")
    ssh = FakeProbeSsh(project.site, tmp_path / "scratch")
    with Database(tmp_path / "edr.db") as db:
        yield project, batch, ssh, db


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


# plan


def test_plan_renders_the_demo_spec(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    plans = launch.plan(project, batch, ssh, db, date=DATE)
    a, b = plans
    assert [p.problems for p in plans] == [[], []] and not a.queued
    assert a.run_id == f"{DATE}_a_demo_gHEAD" and b.run_id == f"{DATE}_b_nodw_demo_DW0_gHEAD"
    root = f"{tmp_path}/scratch/{getpass.getuser()}/edr/demo/{a.run_id}"
    assert a.host == "local" and a.root == root
    spec = a.spec
    assert set(spec) == SPEC_KEYS and spec["schema"] == 1 and spec["shell"] == "/bin/bash"
    assert spec["state_file"] == f"{tmp_path}/state/demo/{a.run_id}.json"
    assert spec["queue_dir"] == f"{tmp_path}/state/demo/{a.run_id}.queue"
    assert spec["limits"] == {"host_free_min_gb": 1, "streak": 2, "heartbeat_s": 1, "gate_max_s": 30, "lease_s": 600}
    assert spec["start_at"] == {"stage": "synth", "checkpoint": None}
    synth, pnr, export, power = spec["stages"]
    assert [s["name"] for s in spec["stages"]] == ["synth", "pnr", "export", "power"]
    assert synth["cmd"].split() == ["bash", f"{root}/flow/flow.sh", "synth", a.run_id, "demo", "LAST_STAGE=synth"]
    assert "FIRST_STAGE={checkpoint}" in synth["resume"] and synth["cwd"] == root
    assert synth["needs"] == {"cores": 1, "disk_gb": 0.1} and synth["steps"] == ["setup", "analyze", "elaborate", "synth"]
    assert synth["tools"] == [{"name": "demo", "seats": 1, "probe": ["bash", f"{root}/flow/seats.sh"],
                               "leases": f"{tmp_path}/state/leases/demo"}]
    assert synth["budget"] == {"hours": 1, "disk_gb": 1} and synth["retry"] == {"match": "licen[cs]e", "wait_s": 1, "max": 2}
    assert "retry" not in pnr and "tools" not in export and "cmd" not in power
    assert power["parallel"] == 2 and power["after_each"] == "rm -f {task_dir}/wave.vcd"
    assert power["tools"] == synth["tools"] and power["budget"] == {"hours": 1, "per": "task"}
    small, big = power["tasks"]
    assert small["dir"] == f"{root}/simulation/tests/demo/GEMM_M64_N64"
    assert small["cmd"] == f"DEMO_CONFIG=demo bash {root}/flow/kernel.sh gemm GEMM_M64_N64 M=64 N=64"
    assert small["needs"] == {"cores": 1, "disk_gb": 0.05} and small["budget"] == {"hours": 1, "per": "task"}
    assert big["budget"] == {"hours": 2, "per": "task"} and "tools" not in small and "tools" not in big
    left = [s for s in strings(spec) if "{" in s.replace("{checkpoint}", "").replace("{task_dir}", "")]
    assert left == []
    assert " DW=0" in b.spec["stages"][0]["cmd"] and b.spec["stages"][0]["cmd"].endswith("DW=0")


def test_plan_takes_the_probes_of_the_caller(env, tmp_path: Path, monkeypatch) -> None:
    project, batch, ssh, db = env
    monkeypatch.setattr(ssh, "probe", lambda host: pytest.fail(f"plan probed {host}"))
    given = {"local": HostProbe("local", 4.0, 8.0, str(tmp_path / "given"), 50.0)}
    a, b = launch.plan(project, batch, ssh, db, date=DATE, probes=given)
    assert a.problems == [] and b.problems == [] and a.root.startswith(f"{tmp_path}/given/")
    a, b = launch.plan(project, batch, ssh, db, date=DATE, probes={})
    assert a.problems == ["local: no probe of the host"] and a.host is None and not a.queued


def test_plan_reports_problems(env) -> None:
    project, batch, ssh, db = env
    batch.jobs[1].overrides = {"bad key": "1"}
    project.stages["export"].cmd += " {nope}"
    a, b = launch.plan(project, batch, ssh, db, date=DATE)
    assert any("nope" in p for p in a.problems) and a.spec == {} and not a.queued
    assert any("'bad key'" in p for p in b.problems)
    batch.jobs[1].overrides = {"DW": "0"}
    batch.jobs[1].stages = ["export"]
    project.stages["export"].cmd = "true"
    _, b = launch.plan(project, batch, ssh, db, date=DATE)
    assert b.problems == ["overrides given, but no stage of the job uses {overrides}"]


def test_plan_needs_the_tools_of_the_host(env) -> None:
    project, batch, ssh, db = env
    synth_only(batch)
    project.stages["synth"].cmd += " TOOL={tool.demo.version}"
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == [] and p.spec["stages"][0]["cmd"].endswith(" TOOL=1.0")
    project.site.hosts["local"].tools = {}
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == ["local lacks the tools: demo"] and p.spec == {}
    batch.jobs[0].host = "auto"
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == ["no host has the tools: demo"] and p.host is None and not p.queued
    project.site.hosts["hostB"] = replace(project.site.hosts["local"], name="hostB", tools=None)
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == [] and p.host == "hostB" and p.spec["stages"][0]["cmd"].endswith(" TOOL=")
    project.site.hosts["local"].tools = None
    project.tasks["k_big"].needs = Needs(tools={"demo": 2})
    batch.jobs[0].stages, batch.jobs[0].tasks, batch.jobs[0].host = ["power"], ["k_small", "k_big"], "local"
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    small, big = p.spec["stages"][0]["tasks"]
    assert "tools" not in small and big["tools"] == [{"name": "demo", "seats": 2, "probe": ["bash", f"{p.root}/flow/seats.sh"],
                                                               "leases": str(project.state_dir / "leases" / "demo")}]


def test_build_tag_default_and_hook(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    a, b = batch.jobs
    assert launch.build_tag(project, a) == "demo" and launch.build_tag(project, b) == "demo_DW0"
    b.overrides = {"N": "8", "DW": "0"}
    assert launch.build_tag(project, b) == "demo_N8_DW0"
    hook = tmp_path / "build_tag.py"
    hook.write_text("def build_tag(config, overrides):\n    return f'{config}-x{len(overrides)}'\n")
    project.source.build_tag = f"python:{hook}:build_tag"
    assert launch.build_tag(project, b) == "demo-x2"


def test_job_vars_fill_the_stage_strings_and_env(env) -> None:
    project, batch, ssh, db = env
    synth_only(batch)
    batch.jobs[0].vars = {}
    project.stages["synth"].cmd += " NETLIST={vars.netlist_stage}"
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == [f"missing placeholder {{vars.netlist_stage}} in '{project.stages['synth'].cmd}'"]
    batch.jobs[0].vars = {"netlist_stage": "7", "corner": "ss"}
    project.env = {"CORNER": "{vars.corner}"}
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == [] and p.spec["stages"][0]["cmd"].endswith(" NETLIST=7")
    assert p.spec["env"]["CORNER"] == "ss" and p.spec["vars"] == {"netlist_stage": "7", "corner": "ss"}


def test_a_job_without_config(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    synth_only(batch)
    job = batch.jobs[0]
    job.config = ""
    assert launch.build_tag(project, job) == ""
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == [] and p.run_id == f"{DATE}_a_gHEAD" and p.spec["config"] == ""
    job.overrides = {"DW": "0"}
    assert launch.build_tag(project, job) == "DW0"
    hook = tmp_path / "tag.py"
    hook.write_text("def build_tag(config, overrides):\n    return repr(config)\n")
    project.source.build_tag = f"python:{hook}:build_tag"
    assert launch.build_tag(project, job) == "''"
    hook.write_text("def build_tag(config, overrides, worktree):\n    return len(None)\n")
    with pytest.raises(ConfigError, match="object of type 'NoneType' has no len"):
        launch.build_tag(project, job)  # the hook's own fault, not a second call with two arguments
    assert launch.render_run_id("{build_tag}_{label}-{config}", {"build_tag": "", "label": "a", "config": ""}) == "a"


def test_plan_reuses_a_database_run(env) -> None:
    project, batch, ssh, db = env
    db.upsert_run({"run_id": f"{DATE}_a_demo_gOLD", "batch": "old", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD"})
    batch.jobs[0].reuse = {"label": "a", "latest": True}
    batch.jobs[0].stages = ["pnr"]
    a = launch.plan(project, batch, ssh, db, date=DATE)[0]
    assert a.problems == [] and a.reuse == f"{DATE}_a_demo_gOLD" and a.src == "OLD"
    assert a.root == "/x/edr/old" and a.run_id == f"{DATE}_a_demo_gOLD"
    assert a.spec["start_at"]["stage"] == "pnr" and a.spec["stages"][0]["cwd"] == "/x/edr/old"
    batch.jobs[0].reuse = {"label": "nope", "latest": True}
    a = launch.plan(project, batch, ssh, db, date=DATE)[0]
    assert a.problems == ["reuse label=nope latest=True: no run with a host and a root in the database"]


# launch and stop


def test_launch_local_runs_synth_to_done(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    synth_only(batch)
    out = launch.launch(project, batch, ssh, db, src_dir=src_tree(tmp_path))
    (row,) = out
    assert row["started"] and row["pid"] and row["problems"] == []
    run_id = row["run_id"]
    state = tmp_path / "state" / "demo"
    assert (state / "RUN_DATE").read_text().strip() == run_id[:13]
    spec = json.loads((state / f"{run_id}.spec.json").read_text())
    assert Path(spec["driver"]).parent == tmp_path / "state" / "bin" and Path(spec["driver"]).is_file()
    assert [s["name"] for s in spec["stages"]] == ["synth"]
    assert spec["record"]["edarunner"] == edarunner.__version__ and len(spec["record"]["driver_sha256"]) == 64
    assert (tmp_path / "state" / "leases" / "demo").is_dir()
    assert (Path(row["root"]) / "flow" / "flow.sh").exists()
    assert db.run(run_id)["state"] == "running" and db.batches()[0]["batch"] == "demo"
    assert [e["kind"] for e in db.events(run_id=run_id)] == ["launch"]
    hb = wait_for({"state_file": str(state / f"{run_id}.json")}, lambda h: h["phase"] == "done", timeout=60)
    assert hb["exit"] == 0 and (Path(row["root"]) / "reports" / "3" / "qor.rpt").exists()
    assert list((tmp_path / "state" / "leases" / "demo").iterdir()) == []
    again = launch.launch(project, batch, ssh, db, src_dir=src_tree(tmp_path / "again"))
    assert not again[0]["started"] and "already launched" in again[0]["problems"][0]


def test_stop_kills_a_running_driver(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    synth_only(batch)
    (row,) = launch.launch(project, batch, ssh, db, src_dir=src_tree(tmp_path))
    hb_path = tmp_path / "state" / "demo" / f"{row['run_id']}.json"
    hb = wait_for({"state_file": str(hb_path)}, lambda h: h["phase"] == "stage:synth", timeout=60)
    run_row = db.run(row["run_id"])
    assert launch.stop(ssh, db, run_row, hb, now=True, grace_s=15, why="test", state=project.state_dir)
    hb = wait_for({"state_file": str(hb_path)}, lambda h: h["exit"] is not None, timeout=20)
    assert hb["phase"] in ("KILLED:SIGTERM", "STOPPED") and hb["exit"] == 10 and hb["pgids"] == []
    assert not ssh.pid_alive("local", hb["driver_pid"])
    assert [e["kind"] for e in db.events(run_id=row["run_id"])] == ["launch", "stop"]
    assert launch.stop(ssh, db, run_row, hb, after_task=True, why="later", state=project.state_dir)
    assert (hb_path.with_name(f"{row['run_id']}.stop")).read_text().strip() == "after-task"


def test_stop_kills_a_process_group_by_pgid(env) -> None:
    project, batch, ssh, db = env
    p = subprocess.Popen(["sleep", "300"], start_new_session=True)
    assert os.getpgid(p.pid) == p.pid
    row = {"run_id": "r", "host": "local", "batch": "demo"}
    assert launch.stop(ssh, db, row, {"driver_pid": None, "pgids": [p.pid]}, why="test")
    assert p.wait(timeout=10) == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.killpg(p.pid, 0)
    assert db.events()[-1]["text"] == f"test [driver None, pgids [{p.pid}], term, ended]"


# dry run and guards


def listing(root: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def test_dry_run_writes_nothing(env, tmp_path: Path, capsys) -> None:
    project, batch, ssh, db = env
    state = project.state_dir
    state.mkdir()
    (state / "demo").mkdir()
    (state / "demo" / "RUN_DATE").write_text(DATE + "\n")
    before = listing(state)
    out = launch.launch(project, batch, ssh, db, dry_run=True, src_dir=src_tree(tmp_path))
    assert listing(state) == before and not (tmp_path / "scratch").exists()
    assert db.runs() == [] and db.events() == [] and db.batches() == []
    assert [r["run_id"] for r in out] == [f"{DATE}_a_demo_gHEAD", f"{DATE}_b_nodw_demo_DW0_gHEAD"]
    assert all(not r["started"] and r["problems"] == [] for r in out)
    text = capsys.readouterr().out
    assert "rsync -a --delete --exclude=.git" in text and f"{DATE}_a_demo_gHEAD" in text
    assert launch.stop(ssh, db, {"run_id": "r", "host": "local", "batch": "demo"}, {"driver_pid": 1, "pgids": [2]},
                       dry_run=True) and db.events() == []


def test_launch_refuses_a_dirty_source(env) -> None:
    project, batch, ssh, db = env
    batch.source = "abc1234-dirty-deadbeef"
    with pytest.raises(Refuse):
        launch.launch(project, batch, ssh, db, dry_run=True)


def test_guard_refuses_a_root_a_top_level_directory_and_home() -> None:
    for bad in ("/", "/x", "/edr", "/edr/", os.path.expanduser("~"), "/a/edr/../.."):
        with pytest.raises(Refuse, match="root"):
            assert_safe_target(bad, "/edr/", 1)
    assert assert_safe_target("/edr/x", "/edr/", 1) == Path("/edr/x")


def test_guard_normalizes_dotdot(tmp_path: Path) -> None:
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
    project, batch, ssh, db = env
    with pytest.raises(Refuse):
        launch.stop(ssh, db, {"run_id": "r", "host": "local", "batch": "demo"}, {"driver_pid": pid, "pgids": []})
    assert db.events() == []


def test_sync_tree_guards_the_target(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    with pytest.raises(Refuse):
        sync.sync_tree(ssh, "local", tmp_path, str(tmp_path / "no-marker"), [], "/edr/", 3)
    assert sync.sync_tree(ssh, "local", tmp_path, str(tmp_path / "a" / "edr" / "b"), [], "/edr/", 3, dry_run=True)
    assert not (tmp_path / "a").exists()


def test_sync_tree_deletes_a_stale_file(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("k")
    target = tmp_path / "a" / "edr" / "b"
    (target / ".git").mkdir(parents=True)
    (target / "stale.txt").write_text("s")
    (target / ".git" / "HEAD").write_text("ref")
    assert sync.sync_tree(ssh, "local", src, str(target), [".git"], "/edr/", 3)
    assert (target / "keep.txt").read_text() == "k" and not (target / "stale.txt").exists()
    assert (target / ".git" / "HEAD").exists()  # an excluded path survives the delete


@pytest.mark.parametrize("excludes", [[], [".venv"], [".git", ".venv"]])
def test_sync_tree_never_copies_the_git_pointer(env, tmp_path: Path, excludes) -> None:
    project, batch, ssh, db = env
    src = tmp_path / "src"
    (src / ".venv").mkdir(parents=True)
    (src / ".git").write_text("gitdir: /head/repo/.git/worktrees/src\n")
    (src / ".venv" / "x").write_text("v")
    (src / "flow.sh").write_text("f")
    target = tmp_path / "a" / "edr" / "b"
    assert sync.sync_tree(ssh, "local", src, str(target), excludes, "/edr/", 3)
    assert (target / "flow.sh").exists() and not (target / ".git").exists()
    assert (target / ".venv").exists() == (".venv" not in excludes)


def test_publish_driver_one_copy_per_version(tmp_path: Path) -> None:
    dest = sync.publish_driver(tmp_path, launch.DRIVER_SRC)
    digest = hashlib.sha256(launch.DRIVER_SRC.read_bytes()).hexdigest()[:8]
    assert dest == tmp_path / "bin" / f"edr_driver-{digest}.py" and dest.read_bytes() == launch.DRIVER_SRC.read_bytes()
    assert [p.name for p in dest.parent.iterdir()] == [dest.name] and dest.stat().st_mode & 0o111
    ino = dest.stat().st_ino
    assert sync.publish_driver(tmp_path, launch.DRIVER_SRC) == dest and dest.stat().st_ino == ino  # reused as is
    v2 = tmp_path / "v2.py"
    v2.write_text("print(2)\n")
    dry = sync.publish_driver(tmp_path, v2, dry_run=True)
    assert dry.parent == dest.parent and dry != dest and [p.name for p in dest.parent.iterdir()] == [dest.name]
    assert sync.publish_driver(tmp_path, v2) == dry and dry.read_text() == "print(2)\n" and dest.stat().st_ino == ino


def test_reuse_renders_tree_id_of_the_old_run(env) -> None:
    project, batch, ssh, db = env
    db.upsert_run({"run_id": "20260101_0000_a_demo_gOLD", "batch": "old", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD"})
    batch.jobs[0].reuse = {"label": "a", "latest": True}
    batch.jobs[0].stages = ["pnr"]
    plans = launch.plan(project, batch, ssh, db, date=DATE)
    a = next(p for p in plans if p.label == "a")
    assert a.values["tree_id"] == "20260101_0000_a_demo_gOLD" and a.values["run_id"] != a.values["tree_id"]
    b = next(p for p in plans if p.label != "a")
    assert b.values["tree_id"] == b.values["run_id"]


def test_restore_launches_a_fresh_tree_from_the_archive(env, tmp_path: Path) -> None:
    project, batch, ssh, db = env
    project.data = tmp_path / "data"
    old = "20260101_0000_a_demo_gOLD"
    db.upsert_run({"run_id": old, "batch": "old", "label": "a", "src": "OLD", "build_tag": "demo", "state": "retired"})
    archive = project.data / "results" / old / "out" / "11"
    archive.mkdir(parents=True)
    (archive / "netlist.v").write_text("module top; endmodule\n")
    synth_only(batch)
    batch.jobs[0].reuse = {"run_id": old, "restore": "netlist"}
    (p,) = launch.plan(project, batch, ssh, db, date=DATE)
    assert p.problems == [] and p.reuse == old and p.restore == "netlist" and p.src == "OLD"
    assert p.host == "local" and p.root and p.values["tree_id"] == old and p.build_tag == "demo"
    (dry,) = launch.launch(project, batch, ssh, db, src_dir=src_tree(tmp_path), dry_run=True)
    assert dry["problems"] == [] and not Path(p.root).exists()
    (row,) = launch.launch(project, batch, ssh, db, src_dir=src_tree(tmp_path / "again"))
    root = Path(row["root"])
    assert row["started"] and (root / "out" / "11" / "netlist.v").is_file() and (root / "flow" / "flow.sh").is_file()
    wait_for({"state_file": str(tmp_path / "state" / "demo" / f"{row['run_id']}.json")}, lambda h: h["phase"] == "done", timeout=60)
    batch.jobs[0].label, batch.jobs[0].reuse = "a_bad", {"run_id": old, "restore": "nope"}
    (bad,) = launch.launch(project, batch, ssh, db, src_dir=src_tree(tmp_path / "bad"))
    assert bad["problems"] == ["sync failed"] and not bad["started"]


def test_remote_driver_uses_the_login_python(tmp_path: Path) -> None:
    class FakeSsh:
        def run(self, host, cmd, timeout_s=None):
            self.cmd = cmd
            return 0, "4242\n", ""
    ssh = FakeSsh()
    req = Request("r", tmp_path / "s.json", tmp_path / "d.py", tmp_path / "l.log", "hostA", {"PATH": "/opt/eda/bin:$PATH"})
    assert SshBackend(ssh).submit(req) == Handle("ssh", "hostA:4242", "hostA")
    assert ssh.cmd.startswith("py=$(command -v python3); setsid nohup \"$py\" ")
    assert "export" not in ssh.cmd and "/opt/eda" not in ssh.cmd and "python3 " not in ssh.cmd.split("nohup")[1]


def test_project_env_is_rendered_over_the_site_env(env) -> None:
    project, batch, ssh, db = env
    project.env = {"PATH": "{root}/.venv/bin:$PATH", "FLOW_TAG": "{build_tag}"}
    a = launch.plan(project, batch, ssh, db, date=DATE)[0]
    e = a.spec["env"]
    assert e["PATH"] == f"{a.root}/.venv/bin:$PATH" and e["FLOW_TAG"] == a.values["build_tag"]
    for k, val in project.site.env.items():
        assert k in e


def test_project_env_builds_on_the_site_env(env) -> None:
    project, batch, ssh, db = env
    project.site.env = {"PATH": "/opt/tools/bin:$PATH", "LM_LICENSE_FILE": "1717@lic", "TOOLS": "/opt/tools"}
    project.env = {"PATH": "{root}/.venv/bin:$PATH", "LM_LICENSE_FILE": "2020@other",
                   "LD_LIBRARY_PATH": "${TOOLS}/lib:$LD_LIBRARY_PATH", "COST": "$$5"}
    a = launch.plan(project, batch, ssh, db, date=DATE)[0]
    e = a.spec["env"]
    assert e["PATH"] == f"{a.root}/.venv/bin:/opt/tools/bin:$PATH"
    assert e["LM_LICENSE_FILE"] == "2020@other"
    assert e["TOOLS"] == "/opt/tools"
    assert e["LD_LIBRARY_PATH"] == "/opt/tools/lib:$LD_LIBRARY_PATH" and e["COST"] == "$$5"


def test_project_env_keeps_a_name_it_sets_itself(env) -> None:
    project, batch, ssh, db = env
    project.site.env = {"TOOLS": "/opt/tools"}
    project.env = {"TOOLS": "/opt/new", "LIB": "$TOOLS/lib"}
    e = launch.plan(project, batch, ssh, db, date=DATE)[0].spec["env"]
    assert e["TOOLS"] == "/opt/new" and e["LIB"] == "$TOOLS/lib"


def test_tree_id_survives_a_chain_of_reuse(env) -> None:
    project, batch, ssh, db = env
    db.upsert_run({"run_id": "20260101_0000_a_demo_gOLD", "batch": "old", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD", "tree_id": "20260101_0000_a_demo_gOLD"})
    db.upsert_run({"run_id": "20260102_0000_a_demo_gOLD", "batch": "mid", "label": "a", "host": "local",
                       "root": "/x/edr/old", "src": "OLD", "tree_id": "20260101_0000_a_demo_gOLD"})
    batch.jobs[0].reuse = {"label": "a", "latest": True}
    batch.jobs[0].stages = ["pnr"]
    a = next(p for p in launch.plan(project, batch, ssh, db, date=DATE) if p.label == "a")
    assert a.reuse == "20260102_0000_a_demo_gOLD" and a.values["tree_id"] == "20260101_0000_a_demo_gOLD"
    assert launch._run_row(a, batch)["tree_id"] == "20260101_0000_a_demo_gOLD"

"""schedulers.py: the rendered submit input against golden files, and the scheduler commands against shims on PATH."""

from __future__ import annotations

import getpass
import shlex
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from edarunner import config, launch
from edarunner.backend import Handle, Live, Request, make_backend
from edarunner.db import Database
from edarunner.hosts import HostError, Ssh
from edarunner.model import Scheduler
from edarunner.schedulers import CondorBackend, LsfBackend, SlurmBackend, condor_submit, lsf_argv, slurm_script
from helpers_driver import DEMO

GOLDEN = Path(__file__).resolve().parent / "golden"
FULL = Request(run_id="r1", spec=Path("/s/b/r1.spec.json"), driver=Path("/s/bin/edr_driver-ab.py"),
               log=Path("/s/b/r1.driver.log"), host="node7", project="demo", cores=4, ram_gb=8, disk_gb=1.5,
               hours=2.5, licences={"fc": 1, "vcs": 2}, queue="long")
BARE = Request(run_id="r2", spec=Path("/s/b/r2.spec.json"), driver=Path("/s/bin/edr_driver-ab.py"),
               log=Path("/s/b/r2.driver.log"))


@pytest.mark.parametrize("name,text", [
    ("condor_full.sub", condor_submit(replace(FULL, options=['+EdrTag = "a"']))), ("condor_bare.sub", condor_submit(BARE)),
    ("slurm_full.sh", slurm_script(replace(FULL, options=["--comment=a"]))), ("slurm_bare.sh", slurm_script(BARE)),
    ("lsf_full.txt", shlex.join(lsf_argv(replace(FULL, options=["-P", "a"]))) + "\n"),
    ("lsf_bare.txt", shlex.join(lsf_argv(BARE)) + "\n"),
])
def test_rendering_matches_the_golden_file(name: str, text: str) -> None:
    assert text == (GOLDEN / name).read_text()


class Shims:
    """Scripts on an otherwise empty PATH: each logs its argv, prints a canned output and exits with a canned code."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.dir, self.log = tmp_path / "shims", tmp_path / "shims.log"
        self.dir.mkdir()
        monkeypatch.setenv("PATH", str(self.dir))
        monkeypatch.setenv("SHIM_LOG", str(self.log))

    def add(self, name: str, out: str = "", rc: int = 0, err: str = "") -> None:
        (self.dir / f"{name}.out").write_text(out)
        (self.dir / f"{name}.err").write_text(err)
        path = self.dir / name
        path.write_text(f'#!/bin/sh\nfor a in {name} "$@"; do printf "%s|" "$a" >> "$SHIM_LOG"; done\n'
                        f'echo >> "$SHIM_LOG"\n/bin/cat "{path}.out"\n/bin/cat "{path}.err" >&2\nexit {rc}\n')
        path.chmod(0o755)

    def calls(self) -> list[list[str]]:
        return [line.split("|")[:-1] for line in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def shims(tmp_path: Path, monkeypatch) -> Shims:
    return Shims(tmp_path, monkeypatch)


def req(tmp_path: Path, run_id: str = "r1") -> Request:
    (tmp_path / "b").mkdir(exist_ok=True)
    return replace(FULL, run_id=run_id, spec=tmp_path / "b" / f"{run_id}.spec.json", log=tmp_path / "b" / f"{run_id}.driver.log")


def test_condor(shims: Shims, tmp_path: Path) -> None:
    b = CondorBackend(Scheduler(backend="condor", tree_root="/t"))
    shims.add("condor_submit", "12.0 - 12.0\n")
    h = b.submit(req(tmp_path))
    sub = tmp_path / "b" / "r1.sub"
    assert h == Handle("condor", "12.0") and str(h) == "condor:12.0" and Handle.parse(str(h)) == h
    assert shims.calls() == [["condor_submit", "-terse", str(sub)]] and "concurrency_limits = fc:1,vcs:2" in sub.read_text()
    shims.add("condor_q", "12 0 2\n13 0 5 disk quota exceeded\n14 0 1\n15 0 7\n16 0 4\n")
    hs = [Handle("condor", f"{i}.0") for i in range(12, 18)]
    assert b.alive(hs) == {hs[0]: (Live.RUNNING, ""), hs[1]: (Live.HELD, "disk quota exceeded"),
                           hs[2]: (Live.PENDING, ""), hs[3]: (Live.SUSPENDED, ""), hs[4]: (Live.GONE, ""),
                           hs[5]: (Live.GONE, "job 17.0 left the queue")}
    assert shims.calls()[-1] == ["condor_q", *(h.id for h in hs), "-af", "ClusterId", "ProcId", "JobStatus", "HoldReason"]
    shims.add("condor_q", rc=1, err="Failed to connect to schedd")
    assert b.alive(hs[:1])[hs[0]][0] is Live.UNKNOWN
    shims.add("condor_rm", "All jobs in cluster 12 have been marked for removal\n")
    b.stop(hs[0], hard=True, pgids=[5])
    assert shims.calls()[-1] == ["condor_rm", "12.0"]
    shims.add("condor_submit", "ERROR: no such file\n", rc=1)
    with pytest.raises(HostError, match="condor_submit: rc 1"):
        b.submit(req(tmp_path))
    assert b.free(["x"]) is None and b.file_host({"host": "node7"}) == "local"


@pytest.mark.parametrize("rc,out,err,known", [(0, "1\n", "", True), (1, "", "Not defined: FC_LIMIT\n", False),
                                              (1, "", "Can't find address for negotiator\n", None)])
def test_condor_licence(shims: Shims, rc: int, out: str, err: str, known: bool | None) -> None:
    shims.add("condor_config_val", out, rc, err)
    assert CondorBackend(Scheduler()).licence("fc") is known
    assert shims.calls() == [["condor_config_val", "-negotiator", "FC_LIMIT"]]


def test_slurm(shims: Shims, tmp_path: Path) -> None:
    b = SlurmBackend(Scheduler(backend="slurm", tree_root="/t"))
    shims.add("sbatch", "77;cluster\n")
    h = b.submit(req(tmp_path))
    script = tmp_path / "b" / "r1.sbatch"
    assert h == Handle("slurm", "77") and shims.calls() == [["sbatch", "--parsable", str(script)]]
    assert "#SBATCH --licenses=fc:1,vcs:2" in script.read_text()
    shims.add("squeue", "77 RUNNING None\n78 PENDING JobHeldUser\n79 PENDING Priority\n80 SUSPENDED None\n")
    shims.add("sacct", "81|COMPLETED\n82|CANCELLED by 0\n")
    hs = [Handle("slurm", str(i)) for i in range(77, 84)]
    got = b.alive(hs)
    assert [got[h][0] for h in hs] == [Live.RUNNING, Live.HELD, Live.PENDING, Live.SUSPENDED, Live.GONE, Live.GONE, Live.GONE]
    assert got[hs[5]][1] == "CANCELLED" and got[hs[6]][1] == "job 83 left the queue"
    assert shims.calls()[-2:] == [["squeue", "-h", "-o", "%i %T %r", "-j", "77,78,79,80,81,82,83"],
                                  ["sacct", "-n", "-P", "-X", "-o", "JobID,State", "-j", "81,82,83"]]
    shims.add("squeue", rc=1, err="slurm_load_jobs error: Invalid job id specified\n")
    shims.add("sacct", "81|RUNNING\n")
    assert b.alive(hs[4:5]) == {hs[4]: (Live.RUNNING, "")}
    shims.add("sacct", rc=1, err="Connection refused\n")
    assert b.alive(hs[4:5])[hs[4]][0] is Live.UNKNOWN
    shims.add("scancel")
    b.stop(hs[0], hard=False)
    b.stop(hs[0], hard=True)
    assert shims.calls()[-2:] == [["scancel", "77"], ["scancel", "-f", "-s", "KILL", "77"]]
    shims.add("scontrol", "LicenseName=fc\n    Total=1 Used=0 Free=1\n")
    assert b.licence("fc") is True
    shims.add("scontrol", rc=1, err="scontrol: error: slurm_load_licenses error: Unable to contact slurm controller\n")
    assert b.licence("fc") is None


def test_lsf(shims: Shims, tmp_path: Path) -> None:
    b = LsfBackend(Scheduler(backend="lsf", tree_root="/t"))
    shims.add("bsub", "Job <5> is submitted to queue <long>.\n")
    r = req(tmp_path)
    assert b.submit(r) == Handle("lsf", "5") and shims.calls() == [lsf_argv(r)]
    shims.add("bjobs", "5 RUN\n6 PSUSP\n8 USUSP\n9 DONE\n", rc=255, err="Job <7> is not found\n")
    hs = [Handle("lsf", str(i)) for i in range(5, 10)]
    assert [b.alive(hs)[h][0] for h in hs] == [Live.RUNNING, Live.HELD, Live.GONE, Live.SUSPENDED, Live.GONE]
    assert shims.calls()[-1] == ["bjobs", "-noheader", "-o", "jobid stat", "5", "6", "7", "8", "9"]
    shims.add("bjobs", rc=255, err="LSF is down. Please wait ...\n")
    assert b.alive(hs[:1])[hs[0]][0] is Live.UNKNOWN
    shims.add("bkill", "Job <5> is being terminated\n")
    b.stop(hs[0], hard=True)
    assert shims.calls()[-1] == ["bkill", "-s", "KILL", "5"] and b.licence("fc") is None


def test_submit_via_prefixes_every_command(shims: Shims) -> None:
    shims.add("ssh", "1 0 2\n")
    shims.add("docker", "1 0 1\n")
    h = Handle("condor", "1.0")
    assert CondorBackend(Scheduler(submit_via=["ssh", "sub"])).alive([h])[h][0] is Live.RUNNING
    assert CondorBackend(Scheduler(submit_via=["docker", "exec", "pool"])).alive([h])[h][0] is Live.PENDING
    q = "condor_q 1.0 -af ClusterId ProcId JobStatus HoldReason"
    assert shims.calls() == [["ssh", "sub", q], ["docker", "exec", "pool", *q.split()]]


@pytest.fixture
def sched_env(tmp_path: Path, shims: Shims, monkeypatch):
    # rsync and mkdir of the tree need the system PATH; no scheduler command lives there.
    monkeypatch.setenv("PATH", f"{shims.dir}:/usr/bin:/bin")
    project = config.load_project(DEMO)
    project.state_dir = tmp_path / "state"
    site = project.site
    site.scheduler = Scheduler(backend="condor", tree_root=str(tmp_path / "shared"), max_jobs=1)
    site.tools["demo"].licence = "fc"
    batch = config.load_batch(project, "demo")
    for j in batch.jobs:
        j.host = "auto"
    shutil.copytree(DEMO / "flow", tmp_path / "src" / "flow")
    with Database(tmp_path / "edr.db") as db:
        yield project, batch, Ssh(site), db


def test_launch_under_a_scheduler(sched_env, tmp_path: Path, shims: Shims) -> None:
    project, batch, ssh, db = sched_env
    backend = make_backend(project.site, ssh)
    assert isinstance(backend, CondorBackend)
    a, b = launch.plan(project, batch, ssh, db, date="20260927_1200", backend=backend)
    assert a.host is None and not a.queued and a.root == f"{tmp_path}/shared/{getpass.getuser()}/edr/demo/{a.run_id}"
    assert a.spec["host"] is None and a.problems == [] and b.queued and not b.spec
    dry = launch.launch(project, batch, ssh, db, dry_run=True, src_dir=tmp_path / "src", backend=backend)
    assert [r["queued"] for r in dry] == [False, True] and shims.calls() == []
    shims.add("condor_submit", "12.0 - 12.0\n")
    rows = launch.launch(project, batch, ssh, db, src_dir=tmp_path / "src", backend=backend)
    assert [(r["started"], r["queued"]) for r in rows] == [(True, False), (False, True)]
    run = db.run(rows[0]["run_id"])
    assert run["handle"] == "condor:12.0" and run["host"] is None and (Path(run["root"]) / "flow" / "flow.sh").is_file()
    sub = (project.state_dir / "demo" / f"{run['run_id']}.sub").read_text()
    # Job a runs the task group power with parallel = 2; export has no budget, so no wall time.
    assert "request_cpus = 2\n" in sub and "concurrency_limits = fc:1\n" in sub and "periodic_remove" not in sub
    assert f"+EdrProject = \"demo\"\n" in sub and "requirements" not in sub
    assert db.run(rows[1]["run_id"])["state"] == "queued"
    hb = {"driver_pid": 99, "sched_id": "12.0", "phase": "stage:synth"}
    shims.add("condor_rm")
    shims.add("condor_q")
    assert launch.stop(ssh, db, run, hb, grace_s=5, backend=backend)
    assert ["condor_rm", "12.0"] in shims.calls()


def test_a_refused_submit_fails_the_run(sched_env, tmp_path: Path, shims: Shims) -> None:
    project, batch, ssh, db = sched_env
    shims.add("condor_submit", rc=1, err="ERROR: Requested node configuration is not available\n")
    row = launch.launch(project, batch, ssh, db, src_dir=tmp_path / "src", backend=make_backend(project.site, ssh))[0]
    assert not row["started"] and row["problems"][0].startswith("submit failed: condor_submit: rc 1")
    assert (db.run(row["run_id"])["phase"], db.run(row["run_id"])["state"]) == ("FAILED:submit", "failed")

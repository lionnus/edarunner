"""schedulers.py: the rendered submit input against golden files, and the scheduler commands against shims on PATH."""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from edarunner.backend import Handle, Live, Request
from edarunner.model import Scheduler
from edarunner.schedulers import LsfBackend, condor_submit, lsf_argv, slurm_script

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

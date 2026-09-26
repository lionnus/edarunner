"""Run identity: the date pin, the build tag, the source tag and the run id.

See docs/design.md sections 2 and 3.1.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import config
from .guards import assert_run_id
from .model import Batch, Job, Project

DATE_FMT = "%Y%m%d_%H%M"


class GitError(Exception):
    """A git command failed. The message holds the command and its stderr."""


def git(*args: str, cwd: Path | None = None) -> str:
    """Run git and return its stdout without the trailing newline."""
    cmd = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    if r.returncode:
        raise GitError(f"{' '.join(cmd)}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.stdout.rstrip("\n")


def batch_date(state: Path, batch: str, dry_run: bool = False) -> str:
    """The pinned date of a batch, YYYYMMDD_HHMM; the first call pins the current minute."""
    pin = Path(state) / batch / "RUN_DATE"
    if pin.exists():
        return pin.read_text().strip()
    date = time.strftime(DATE_FMT)
    if not dry_run:
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(date + "\n")
    return date


def build_tag(project: Project, job: Job) -> str:
    """The build tag of a job: the hook's answer, else <config>[_KEY-VALUE ...] sorted."""
    if project.source.build_tag:
        fn, _ = config.load_hook(project.root, project.source.build_tag)
        return str(fn(job.config, job.overrides))
    pairs = [f"{k}-{v}" for k, v in sorted(job.overrides.items())]
    return "_".join([job.config, *pairs])


def diff(tree: Path) -> str:
    """The `git diff HEAD` of a tree, staged and unstaged changes together."""
    return git("--no-optional-locks", "diff", "HEAD", cwd=tree)


def src_tag(tree: Path) -> str:
    """"<short hash>", or "<short hash>-dirty-<8 hex>" of the sha256 of the diff."""
    tree = Path(tree)
    meta = tree / "source.json"
    if not (tree / ".git").exists() and meta.exists():
        # A snapshot has no .git; its tag is in the file `stagectl` wrote.
        return str(json.loads(meta.read_text())["src"])
    head = git("rev-parse", "--short", "HEAD", cwd=tree)
    text = diff(tree)
    if not text:
        return head
    return f"{head}-dirty-{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def run_id(project: Project, batch: Batch | str, job: Job, src: str, date: str) -> str:
    """Render source.run_id of the project for one job."""
    values = config.placeholders(
        project,
        date=date,
        batch=batch.batch if isinstance(batch, Batch) else batch,
        label=job.label,
        config=job.config,
        build_tag=build_tag(project, job),
        src=src,
        overrides=job.overrides,
    )
    return assert_run_id(config.render(project.source.run_id, values))

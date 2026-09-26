"""`edr stage`: a detached worktree per ref, or a snapshot of a dirty tree.

See docs/design.md sections 3.1 and 14. A dry run prints and writes nothing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import runid
from .guards import assert_safe_target
from .model import Project

SRC_RE = re.compile(r"^[0-9a-f]+(-dirty-[0-9a-f]{8})?$")


class StageError(Exception):
    """The source cannot be staged or found. The message says why."""


@dataclass
class StageResult:
    path: Path
    src: str
    nested: dict[str, str] = field(default_factory=dict)
    dirty: bool = False


def stage(
    project: Project, ref: str | None = None, dirty_dir: Path | None = None, dry_run: bool = False
) -> StageResult:
    """Stage a ref as a worktree, or a dirty directory as a snapshot."""
    if dirty_dir is not None:
        return _snapshot(project, Path(os.path.abspath(Path(dirty_dir).expanduser())), dry_run)
    return _worktree(project, ref or project.source.ref, dry_run)


def find(project: Project, src: str) -> Path:
    """The staged tree of a src tag; a ref resolves to its short hash."""
    wts = project.source.worktrees
    if SRC_RE.match(src) and (wts / src).is_dir():
        return wts / src
    try:
        short = _short(project.source.repo, src)
    except runid.GitError:
        short = ""
    if short and (wts / short).is_dir():
        return wts / short
    raise StageError(f"'{src}' is not staged; run: edr stage {src}")


def remove(project: Project, src: str, dry_run: bool = False) -> Path:
    """Remove the staged tree of a src tag after the safety guard."""
    if not SRC_RE.match(src):
        raise StageError(f"'{src}' is not a src tag")
    path = assert_safe_target(project.source.worktrees / src, project.safety.marker, project.safety.min_depth)
    if not path.is_dir():
        raise StageError(f"'{src}' is not staged")
    if dry_run:
        print(f"dry-run: remove {path}")
        return path
    shutil.rmtree(path)
    runid.git("worktree", "prune", cwd=project.source.repo)
    return path


# --- internals


def _short(repo: Path, ref: str) -> str:
    return runid.git("rev-parse", "--short", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)


def _worktree(project: Project, ref: str, dry_run: bool) -> StageResult:
    repo, wts = project.source.repo, project.source.worktrees
    if runid.git("remote", cwd=repo):
        if dry_run:
            print(f"dry-run: git -C {repo} fetch")
        else:
            runid.git("fetch", "-q", cwd=repo)
    src = _short(repo, ref)
    path = wts / src
    if (path / ".git").exists():
        pass
    elif dry_run:
        print(f"dry-run: git -C {repo} worktree add --detach {path} {src}")
    else:
        wts.mkdir(parents=True, exist_ok=True)
        # A registered worktree whose directory is gone blocks `add`.
        runid.git("worktree", "prune", cwd=repo)
        runid.git("worktree", "add", "-q", "--detach", str(path), src, cwd=repo)
    nested = {n: _nested(repo / n, path / n, dry_run) for n in project.source.nested}
    return StageResult(path, src, {k: v for k, v in nested.items() if v}, False)


def _nested(src: Path, dst: Path, dry_run: bool) -> str:
    """Clone <repo>/<name> into the worktree at the HEAD the repo copy has; '' when absent."""
    if not (src / ".git").exists():
        return ""
    if (dst / ".git").exists():
        return runid.git("rev-parse", "--short", "HEAD", cwd=dst)
    head = runid.git("rev-parse", "--short", "HEAD", cwd=src)
    if dry_run:
        print(f"dry-run: git clone {src} {dst} && git -C {dst} checkout --detach {head}")
        return head
    runid.git("clone", "-q", str(src), str(dst))
    runid.git("checkout", "-q", "--detach", head, cwd=dst)
    return head


def _snapshot(project: Project, tree: Path, dry_run: bool) -> StageResult:
    src = runid.src_tag(tree)
    if "-dirty-" not in src:
        # A clean tree pins its commit; a snapshot would collide with that worktree.
        return _worktree(project, src, dry_run)
    path = project.source.worktrees / src
    nested = {
        n: runid.git("rev-parse", "--short", "HEAD", cwd=tree / n)
        for n in project.source.nested
        if (tree / n / ".git").exists()
    }
    excludes = [a for e in [".git", *project.sync.exclude] for a in ("--exclude", e)]
    cmd = ["rsync", "-a", *excludes, f"{tree}/", f"{path}/"]
    if dry_run:
        print(f"dry-run: {' '.join(cmd)}")
        return StageResult(path, src, nested, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if r.returncode:
        raise StageError(f"{' '.join(cmd)}: {r.stdout.strip()}")
    meta = {
        "src": src,
        "base": src.split("-dirty-")[0],
        "dirty": True,
        "nested": nested,
        "origin": str(tree),
        "created": int(time.time()),
    }
    (path / "source.diff").write_text(runid.diff(tree) + "\n")
    (path / "source.json").write_text(json.dumps(meta, indent=1) + "\n")
    return StageResult(path, src, nested, True)

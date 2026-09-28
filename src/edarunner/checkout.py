"""`edr checkout`: a detached local clone per ref, or a snapshot of a dirty tree.

A clone has a real `.git` directory, so git works on the copy of the tree on a host.

A dry run prints and writes nothing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import runid
from .model import Project

SRC_RE = re.compile(r"^[0-9a-f]+(-dirty-[0-9a-f]{8})?$")


class CheckoutError(Exception):
    """The source cannot be checked out or found. The message says why."""


@dataclass
class CheckoutResult:
    """A checked-out source: its path, its tag, the short hash of each nested repository, and whether it is dirty."""

    path: Path
    src: str
    nested: dict[str, str] = field(default_factory=dict)
    dirty: bool = False


def checkout(
    project: Project, ref: str | None = None, dirty_dir: Path | None = None, dry_run: bool = False
) -> CheckoutResult:
    """Check out a ref as a clone, or copy a dirty directory as a snapshot."""
    if dirty_dir is not None:
        return _snapshot(project, Path(os.path.abspath(Path(dirty_dir).expanduser())), dry_run)
    return _pinned(project, ref or project.source.ref, dry_run)


def find(project: Project, src: str) -> Path:
    """The checked-out tree of a src tag; a ref resolves to its short hash."""
    wts = project.source.worktrees
    if SRC_RE.match(src) and (wts / src).is_dir():
        return wts / src
    try:
        short = _short(project.source.repo, src)
    except runid.GitError:
        short = ""
    if short and (wts / short).is_dir():
        return wts / short
    raise CheckoutError(f"'{src}' is not checked out; run: edr checkout {src}")


def ensure(project: Project, src: str, dry_run: bool = False) -> CheckoutResult | None:
    """Check out a clean source that is not checked out yet; None when it is. A dirty tag is refused."""
    try:
        find(project, src)
        return None
    except CheckoutError:
        if "-dirty-" in src:
            raise CheckoutError(f"'{src}' is a dirty snapshot that is not checked out; "
                                f"run: edr checkout --dirty <tree>") from None
    try:
        return checkout(project, src, dry_run=dry_run)
    except runid.GitError as e:
        raise CheckoutError(f"'{src}' is not checked out and does not resolve: {e}") from None


# --- internals


def _short(repo: Path, ref: str) -> str:
    return runid.git("rev-parse", "--short", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)


def _clone(src: Path, dst: Path, commit: str, dry_run: bool) -> None:
    """Clone `src` into `dst` at `commit`, detached; `--local` hardlinks the objects."""
    if dry_run:
        print(f"dry: git clone --local --no-checkout {src} {dst} && git -C {dst} checkout --detach {commit}")
        return
    runid.git("clone", "-q", "--local", "--no-checkout", str(src), str(dst))
    runid.git("checkout", "-q", "--detach", commit, cwd=dst)
    # The clone of a clone would fetch from the repo copy; point it at the upstream instead.
    try:
        upstream = runid.git("remote", "get-url", "origin", cwd=src)
    except runid.GitError:
        upstream = ""
    if upstream:
        runid.git("remote", "set-url", "origin", upstream, cwd=dst)


def _pinned(project: Project, ref: str, dry_run: bool) -> CheckoutResult:
    repo, wts = project.source.repo, project.source.worktrees
    if runid.git("remote", cwd=repo):
        if dry_run:
            print(f"dry: git -C {repo} fetch")
        else:
            runid.git("fetch", "-q", cwd=repo)
    src = _short(repo, ref)
    path = wts / src
    if not (path / ".git").exists():
        if not dry_run:
            wts.mkdir(parents=True, exist_ok=True)
        _clone(repo, path, src, dry_run)
    nested = {n: _nested(repo / n, path / n, dry_run) for n in project.source.nested}
    return CheckoutResult(path, src, {k: v for k, v in nested.items() if v}, False)


def _nested(src: Path, dst: Path, dry_run: bool) -> str:
    """Clone <repo>/<name> into the clone at the HEAD the repo copy has; '' when absent."""
    if not (src / ".git").exists():
        return ""
    if (dst / ".git").exists():
        return runid.git("rev-parse", "--short", "HEAD", cwd=dst)
    head = runid.git("rev-parse", "--short", "HEAD", cwd=src)
    _clone(src, dst, head, dry_run)
    return head


def _snapshot(project: Project, tree: Path, dry_run: bool) -> CheckoutResult:
    """A clone at the HEAD of `tree` with the files of `tree` copied over it, so git on the copy sees the changes."""
    src = runid.src_tag(tree)
    if "-dirty-" not in src:
        # A clean tree pins its commit; a snapshot would collide with that clone.
        return _pinned(project, src, dry_run)
    path = project.source.worktrees / src
    base = src.split("-dirty-")[0]
    nested = {
        n: runid.git("rev-parse", "--short", "HEAD", cwd=tree / n)
        for n in project.source.nested
        if (tree / n / ".git").exists()
    }
    excludes = [a for e in [".git", *project.sync.exclude] for a in ("--exclude", e)]
    cmd = ["rsync", "-a", *excludes, f"{tree}/", f"{path}/"]
    fresh = not (path / ".git").exists()
    if fresh and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    if fresh:
        _clone(tree, path, base, dry_run)
        for n in nested:
            _nested(tree / n, path / n, dry_run)
    if dry_run:
        print(f"dry: {' '.join(cmd)}")
        return CheckoutResult(path, src, nested, True)
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if r.returncode:
        raise CheckoutError(f"{' '.join(cmd)}: {r.stdout.strip()}")
    # rsync adds and changes files; a file the tree deleted goes here, one path at a time.
    for rel in runid.git("ls-files", "--deleted", "-z", cwd=tree).split("\0"):
        if rel and (path / rel).is_file():
            (path / rel).unlink()
    meta = {
        "src": src,
        "base": base,
        "dirty": True,
        "nested": nested,
        "origin": str(tree),
        "created": int(time.time()),
    }
    (path / "source.diff").write_text(runid.diff(tree) + "\n")
    (path / "source.json").write_text(json.dumps(meta, indent=1) + "\n")
    return CheckoutResult(path, src, nested, True)

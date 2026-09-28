"""`edr checkout`: a detached local clone per ref, or a snapshot of a dirty tree.

A clone has a real `.git` directory, so git works on the copy of the tree on a host.

A dry run prints and writes nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import runid
from .model import Project


class CheckoutError(Exception):
    """The source cannot be checked out or found. The message says why."""


@dataclass
class CheckoutResult:
    """A checked-out source: its path, its tag, the short hash of each nested repository, and whether it is dirty."""

    path: Path
    source: str
    nested: dict[str, str] = field(default_factory=dict)
    dirty: bool = False


def checkout(
    project: Project, ref: str | None = None, dirty_dir: Path | None = None, dry_run: bool = False
) -> CheckoutResult:
    """Check out a ref as a clone, or copy a dirty directory as a snapshot."""
    if dirty_dir is not None:
        return _snapshot(project, Path(os.path.abspath(Path(dirty_dir).expanduser())), dry_run)
    return _pinned(project, ref or project.source.ref, dry_run)


def find(project: Project, source: str) -> Path:
    """The checked-out tree of a source tag; a ref resolves to the tag that its checkout makes."""
    wts = project.source.worktrees
    if runid.parse_tag(source, project.source.nested) and (wts / source).is_dir():
        return wts / source
    try:
        tag = str(_resolve(project, source))
    except runid.GitError:
        tag = ""
    if tag and (wts / tag).is_dir():
        return wts / tag
    raise CheckoutError(f"'{source}' is not checked out; run: edr checkout {source}")


def ensure(project: Project, source: str, dry_run: bool = False) -> CheckoutResult | None:
    """Check out a clean source that is not checked out yet; None when it is. A dirty tag is refused."""
    try:
        find(project, source)
        return None
    except CheckoutError:
        tag = runid.parse_tag(source, project.source.nested)
        if tag and tag.dirty:
            raise CheckoutError(f"'{source}' is a dirty snapshot that is not checked out; "
                                f"run: edr checkout --dirty <tree>") from None
    try:
        return checkout(project, source, dry_run=dry_run)
    except runid.GitError as e:
        raise CheckoutError(f"'{source}' is not checked out and does not resolve: {e}") from None


# --- internals


def _short(repo: Path, ref: str) -> str:
    return runid.git("rev-parse", "--short", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)


def _resolve(project: Project, ref: str) -> runid.Tag:
    """The clean tag that the checkout of `ref` makes. A clean tag names every commit; any other ref takes each nested
    repository at the HEAD that the repository copy has."""
    repo, names = project.source.repo, project.source.nested
    tag = runid.parse_tag(ref, names)
    base, heads = (tag.base, tag.nested) if tag and not tag.dirty else (ref, dict.fromkeys(names, "HEAD"))
    return runid.Tag(_short(repo, base), {n: _short(runid.nested_repo(repo, n), h) for n, h in heads.items()})


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
    tag = _resolve(project, ref)
    path = wts / str(tag)
    if not (path / ".git").exists():
        if not dry_run:
            wts.mkdir(parents=True, exist_ok=True)
        _clone(repo, path, tag.base, dry_run)
    for n, head in tag.nested.items():
        if not (path / n / ".git").exists():
            _clone(repo / n, path / n, head, dry_run)
    return CheckoutResult(path, str(tag), tag.nested, False)


def _snapshot(project: Project, tree: Path, dry_run: bool) -> CheckoutResult:
    """A clone of the commits of `tree` with the files its tag covers copied over it, so git on the copy sees the
    changes.

    `source.diff` and `source.json` go into the clone and into `data/sources/<tag>/`, which `retire` keeps.
    """
    names = project.source.nested
    source = runid.source_tag(tree, names)
    tag = runid.parse_tag(source, names)
    if not (tag and tag.dirty):
        # A clean tree pins its commits; a snapshot would collide with that clone.
        return _pinned(project, source, dry_run)
    path = project.source.worktrees / source
    # An edit of the same size, made in the second the clone wrote the file, passes rsync's size and mtime check.
    cmd = ["rsync", "-a", "--checksum", "--from0", "--files-from=-", f"{tree}/", f"{path}/"]
    fresh = not (path / ".git").exists()
    if fresh and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    if fresh:
        _clone(tree, path, tag.base, dry_run)
        for n, head in tag.nested.items():
            _clone(tree / n, path / n, head, dry_run)
    if dry_run:
        print(f"dry: {' '.join(cmd)}")
        return CheckoutResult(path, source, tag.nested, True)
    # rsync adds and changes files; each file the diff deletes goes first, so a file of the tree can replace its
    # directory. A file that `git rm --cached` left in the tree stays.
    for sub in ["", *tag.nested]:
        diff = ["--no-optional-locks", "diff", "--name-only", "--no-renames", "--diff-filter=D", "-z", "HEAD"]
        for rel in runid.git(*diff, cwd=tree / sub).split("\0"):
            dst = path / sub / rel
            if rel and not os.path.lexists(tree / sub / rel) and (dst.is_symlink() or dst.is_file()):
                dst.unlink()
    r = subprocess.run(cmd, input="\0".join(runid.files(tree, names)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace")
    if r.returncode:
        raise CheckoutError(f"{' '.join(cmd)}: {r.stdout.strip()}")
    meta = {
        "source": source,
        "base": tag.base,
        "dirty": True,
        "nested": tag.nested,
        "origin": str(tree),
        "created": int(time.time()),
    }
    text = runid.diff(tree, names) + "\n"
    for d in (path, project.data / "sources" / source):
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.diff").write_text(text)
        (d / "source.json").write_text(json.dumps(meta, indent=1) + "\n")
    return CheckoutResult(path, source, tag.nested, True)

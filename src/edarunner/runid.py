"""The git wrapper and the source tag of a checked-out tree."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path


class GitError(Exception):
    """A git command failed. The message holds the command and its stderr."""


def git(*args: str, cwd: Path | None = None, ok: tuple[int, ...] = (0,)) -> str:
    """Run git and return its stdout without the trailing newline; an exit code outside `ok` raises GitError."""
    cmd = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    if r.returncode not in ok:
        raise GitError(f"{' '.join(cmd)}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.stdout.rstrip("\n")


def diff(tree: Path, nested: Iterable[str]) -> str:
    """The changes of a tree against its HEAD as one patch: `git diff HEAD`, staged and unstaged changes together,
    then each untracked file that git does not ignore as a new file, then the same for each nested repository with
    its paths under its directory."""
    parts = [_changes(tree, ""), *(_changes(tree / n, f"{n}/") for n in nested if (tree / n / ".git").exists())]
    return "\n".join(p for p in parts if p)


def _changes(repo: Path, prefix: str) -> str:
    opts = ["--no-optional-locks", "diff", f"--src-prefix=a/{prefix}", f"--dst-prefix=b/{prefix}"]
    parts = [git(*opts, "HEAD", cwd=repo)]
    for rel in git("--no-optional-locks", "ls-files", "--others", "--exclude-standard", "-z", cwd=repo).split("\0"):
        # git lists a repository inside the tree as its directory; the nested patch covers a nested one.
        if rel and not rel.endswith("/"):
            parts.append(git(*opts, "--no-index", "--", "/dev/null", rel, cwd=repo, ok=(0, 1)))
    return "\n".join(p for p in parts if p)


def source_tag(tree: Path, nested: Iterable[str]) -> str:
    """"<short hash>", or "<short hash>-dirty-<8 hex>" of the sha256 of `diff` with the nested repositories."""
    tree = Path(tree)
    meta = tree / "source.json"
    if meta.exists():
        # A snapshot keeps its tag in the file `checkout` wrote; git on the copy may not see every change.
        data = json.loads(meta.read_text())
        if not (tree / ".git").exists() or data.get("dirty"):
            return str(data["source"])
    head = git("rev-parse", "--short", "HEAD", cwd=tree)
    text = diff(tree, nested)
    if not text:
        return head
    return f"{head}-dirty-{hashlib.sha256(text.encode()).hexdigest()[:8]}"

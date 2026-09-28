"""The git wrapper and the source tag of a checked-out tree."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


class GitError(Exception):
    """A git command failed. The message holds the command and its stderr."""


def git(*args: str, cwd: Path | None = None) -> str:
    """Run git and return its stdout without the trailing newline."""
    cmd = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    if r.returncode:
        raise GitError(f"{' '.join(cmd)}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.stdout.rstrip("\n")


def diff(tree: Path) -> str:
    """The `git diff HEAD` of a tree, staged and unstaged changes together."""
    return git("--no-optional-locks", "diff", "HEAD", cwd=tree)


def source_tag(tree: Path) -> str:
    """"<short hash>", or "<short hash>-dirty-<8 hex>" of the sha256 of the diff."""
    tree = Path(tree)
    meta = tree / "source.json"
    if meta.exists():
        # A snapshot keeps its tag in the file `checkout` wrote; git on the copy may not see every change.
        data = json.loads(meta.read_text())
        if not (tree / ".git").exists() or data.get("dirty"):
            return str(data["source"])
    head = git("rev-parse", "--short", "HEAD", cwd=tree)
    text = diff(tree)
    if not text:
        return head
    return f"{head}-dirty-{hashlib.sha256(text.encode()).hexdigest()[:8]}"

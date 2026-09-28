"""The git wrapper and the source tag of a checked-out tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

TAG_RE = re.compile(r"([0-9a-f]+)((?:-n[0-9a-f]+)*)(?:-dirty-([0-9a-f]{8}))?")


class GitError(Exception):
    """A git command failed. The message holds the command and its stderr."""


@dataclass
class Tag:
    """The parts of a source tag: the short hash of the tree, the short hash of each nested repository by name, and
    the 8 hex digits of the sha256 of the diff when the tree or a nested repository has changes, else ''."""

    base: str
    nested: dict[str, str] = field(default_factory=dict)
    dirty: str = ""

    def __str__(self) -> str:
        return self.base + "".join(f"-n{h}" for h in self.nested.values()) + (f"-dirty-{self.dirty}" if self.dirty else "")


def parse_tag(text: str, nested: Sequence[str]) -> Tag | None:
    """The parts of a tag that `source_tag` makes with the nested repositories `nested`; None for any other text."""
    m = TAG_RE.fullmatch(text)
    heads = m[2].split("-n")[1:] if m else []
    return Tag(m[1], dict(zip(nested, heads)), m[3] or "") if m and len(heads) == len(nested) else None


def git(*args: str, cwd: Path | None = None, ok: tuple[int, ...] = (0,)) -> str:
    """Run git and return its stdout without the trailing newline; an exit code outside `ok` raises GitError."""
    cmd = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    if r.returncode not in ok:
        raise GitError(f"{' '.join(cmd)}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.stdout.rstrip("\n")


def nested_repo(tree: Path, name: str) -> Path:
    """The nested repository `name` of a tree; GitError when it is missing, since git would answer for the tree
    around it."""
    if not (tree / name / ".git").exists():
        raise GitError(f"[source] nested names {name}, but {tree / name} is not a git repository")
    return tree / name


def diff(tree: Path, nested: Sequence[str]) -> str:
    """The changes of a tree against its HEAD as one patch: `git diff HEAD`, staged and unstaged changes together,
    then each untracked file that git does not ignore as a new file, then the same for each nested repository with
    its paths under its directory."""
    parts = [_changes(tree, "", nested), *(_changes(tree / n, f"{n}/", nested) for n in nested)]
    return "\n".join(p for p in parts if p)


def files(tree: Path, nested: Sequence[str]) -> list[str]:
    """The files that the tag of a tree covers: the tracked files that exist and the untracked files that git does not
    ignore, then the same for each nested repository with its paths under its directory."""
    return [f"{sub}{p}" for sub in ["", *(f"{n}/" for n in nested)]
            for p in [*_ls(tree / sub, "--cached"), *_untracked(tree / sub, sub, nested)] if os.path.lexists(tree / sub / p)]


def _ls(repo: Path, *opts: str) -> list[str]:
    return [p for p in git("--no-optional-locks", "ls-files", "-z", *opts, cwd=repo).split("\0") if p]


def _untracked(repo: Path, prefix: str, nested: Sequence[str]) -> list[str]:
    """The untracked files of a repository that git does not ignore. git lists a repository inside it as its
    directory; one that `nested` does not name raises GitError, since no tag covers its files."""
    out = []
    for rel in _ls(repo, "--others", "--exclude-standard"):
        if not rel.endswith("/"):
            out.append(rel)
        elif prefix + rel[:-1] not in nested:
            raise GitError(f"{repo / rel} is a git repository that [source] nested does not name; "
                           "add it to nested, or ignore it in .gitignore")
    return out


def _changes(repo: Path, prefix: str, nested: Sequence[str]) -> str:
    opts = ["--no-optional-locks", "diff", f"--src-prefix=a/{prefix}", f"--dst-prefix=b/{prefix}"]
    parts = [git(*opts, "HEAD", cwd=repo)]
    parts += [git(*opts, "--no-index", "--", "/dev/null", rel, cwd=repo, ok=(0, 1)) for rel in _untracked(repo, prefix, nested)]
    return "\n".join(p for p in parts if p)


def source_tag(tree: Path, nested: Sequence[str]) -> str:
    """The short hash of the tree, then `-n<short hash>` for each nested repository in the order of `nested`, then
    `-dirty-<8 hex>` of the sha256 of `diff` when the tree or a nested repository has changes."""
    tree = Path(tree)
    meta = tree / "source.json"
    if meta.exists():
        # A snapshot keeps its tag in the file `checkout` wrote; git on the copy may not see every change.
        data = json.loads(meta.read_text())
        if not (tree / ".git").exists() or data.get("dirty"):
            return str(data["source"])
    head = git("rev-parse", "--short", "HEAD", cwd=tree)
    heads = {n: git("rev-parse", "--short", "HEAD", cwd=nested_repo(tree, n)) for n in nested}
    text = diff(tree, nested)
    return str(Tag(head, heads, hashlib.sha256(text.encode()).hexdigest()[:8] if text else ""))

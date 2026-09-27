"""The two guards every delete and every `rsync --delete` call first.

A wrong variable then stops the command instead of removing the wrong tree.
See docs/design.md section 12.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

RUN_ID_RE = re.compile(r"^\d{8}_\d{4}_\S+$")


class Refuse(Exception):
    """A guard refused. The message says why. Exit code 1."""


def assert_safe_target(path: str | os.PathLike, marker: str, min_depth: int = 4) -> Path:
    """Refuse an empty, relative, root, top-level, home, marker-less or shallow path.

    Returns the path as a `Path` when it passes.
    """
    text = os.fspath(path) if path is not None else ""
    if not text:
        raise Refuse("empty path")
    if not text.startswith("/"):
        raise Refuse(f"'{text}' is not absolute")
    # A '..' segment would carry the marker and the depth past the tree it names.
    norm = posixpath.normpath(text)
    # A path with one component (/x) is a filesystem root or a mount, never a run tree.
    if norm.count("/") < 2 or norm == os.path.expanduser("~"):
        raise Refuse(f"'{text}' is a root or a top-level directory, not a target")
    if not marker:
        raise Refuse("empty safety marker")
    if marker not in norm:
        raise Refuse(f"'{text}' does not contain the marker '{marker}'")
    depth = norm.count("/")
    if depth < min_depth:
        raise Refuse(f"'{text}' is too shallow (depth {depth}, need {min_depth})")
    return Path(norm)


def assert_run_id(run_id: str) -> str:
    """Refuse an id that does not start with YYYYMMDD_HHMM_."""
    if not run_id or not RUN_ID_RE.match(run_id):
        raise Refuse(f"'{run_id}' is not a run id")
    return run_id

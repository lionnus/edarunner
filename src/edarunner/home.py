"""The user root, `~/.edr` or `EDR_HOME`: the registry of the projects and the process locks.

`projects/<name>` is a link to the directory of each project, so the commands that span
projects find them. A lock is an `flock` that a process holds until it exits; the lock file
holds its pid.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tomllib
from collections.abc import Iterator
from pathlib import Path

from .guards import Refuse


def root() -> Path:
    """The user root: `EDR_HOME`, else `~/.edr`."""
    return Path(os.environ.get("EDR_HOME") or "~/.edr").expanduser()


def link(name: str) -> Path:
    """The registry link of the project `name`."""
    return root() / "projects" / name


def owner(name: str) -> Path | None:
    """The directory the project name `name` belongs to, or None when the name is free.

    A link to a directory whose `edr.toml` is gone or names another project frees the name."""
    path = link(name)
    if not path.is_symlink():
        return None
    target = Path(os.path.realpath(path))
    try:
        with open(target / "edr.toml", "rb") as f:
            return target if tomllib.load(f).get("project") == name else None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def clash(name: str, directory: Path) -> str:
    """Why `directory` cannot have the name `name`; "" when it can."""
    held = owner(name)
    if held is None or held == Path(os.path.realpath(directory)):
        return ""
    return f"the project name {name} belongs to {held}; rename `project` in {Path(directory) / 'edr.toml'}"


def register(name: str, directory: Path, dry_run: bool = False) -> bool:
    """Link `directory` as the project `name`; True when the link is new. A name of another directory is refused."""
    if why := clash(name, directory):
        raise Refuse(why)
    if owner(name) is not None:
        return False
    if not dry_run:
        path = link(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.{os.getpid()}")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(os.path.realpath(directory))
        # ponytail: two first registrations of one name at once; the later rename wins.
        os.replace(tmp, path)
    return True


def unregister(name: str, directory: Path, dry_run: bool = False) -> bool:
    """Remove the link of `name`, unless it names another project directory; True when there was a link."""
    if why := clash(name, directory):
        raise Refuse(why)
    path = link(name)
    if not path.is_symlink():
        return False
    if not dry_run:
        path.unlink()
    return True


def lock(path: Path) -> int | None:
    """Take the flock of `path` for this process and write its pid into the file; None while another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


@contextlib.contextmanager
def held(path: Path) -> Iterator[None]:
    """Hold the flock of `path` for the block, and wait for it when another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def holder(path: Path) -> str:
    """The pid in a lock file, or `?`."""
    try:
        return path.read_text().strip() or "?"
    except OSError:
        return "?"

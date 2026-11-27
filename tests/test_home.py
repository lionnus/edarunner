"""home.py: the flock and the pid file of a lock under the user root of the test."""

import fcntl
import os

from edarunner import home


def _unreadable(fd: int, n: int) -> bytes:
    raise PermissionError(13, "a read of a locked file over NFS")


def test_holder_takes_the_pid_from_the_pid_file_and_never_reads_the_lock_file(user_root, monkeypatch) -> None:
    path = user_root / "watch.lock"
    assert home.holder(path) is None
    fd = home.lock(path)
    assert fd is not None and path.stat().st_size == 0 and (user_root / "watch.pid").read_text() == f"{os.getpid()}\n"
    monkeypatch.setattr(os, "read", _unreadable)
    assert home.holder(path) == str(os.getpid())
    (user_root / "watch.pid").unlink()
    assert home.holder(path) == "?"
    os.close(fd)
    assert home.holder(path) is None


def test_a_stale_pid_file_names_no_holder_and_a_lock_of_an_older_edr_has_one(user_root, monkeypatch) -> None:
    path, pid = user_root / "watch.lock", user_root / "watch.pid"
    user_root.mkdir()
    pid.write_text("4711\n")
    path.touch()
    assert home.holder(path) is None
    old = os.open(path, os.O_RDWR)
    fcntl.flock(old, fcntl.LOCK_EX)
    os.write(old, b"4711\n")
    pid.unlink()
    monkeypatch.setattr(os, "read", _unreadable)
    assert home.holder(path) == "?"
    os.close(old)

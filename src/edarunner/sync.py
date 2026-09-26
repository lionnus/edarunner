"""Copy the driver by rename and the source tree by rsync. See docs/design.md sections 2 and 6."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config
from .guards import assert_safe_target
from .hosts import Ssh
from .model import Project, Site


def publish_driver(state: Path, batch: str, driver_src: Path, dry_run: bool = False) -> Path:
    """Copy the driver to <state>/bin/<batch>/edr_driver.py by a temporary file and rename."""
    dest = Path(state) / "bin" / batch / "edr_driver.py"
    if dry_run:
        print(f"dry: publish {driver_src} -> {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A write in place keeps the inode a live driver reads; the rename gives a new one.
    fd, tmp = tempfile.mkstemp(prefix=".edr_driver.", dir=dest.parent)
    os.close(fd)
    try:
        shutil.copyfile(driver_src, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest


def sync_tree(
    ssh: Ssh,
    host: str,
    src_dir: Path,
    root: str | os.PathLike,
    excludes: list[str],
    marker: str,
    min_depth: int,
    dry_run: bool = False,
) -> bool:
    """rsync `src_dir` to <host>:<root>/ with --delete; the guard runs first."""
    target = assert_safe_target(root, marker, min_depth)
    src = os.fspath(src_dir).rstrip("/") + "/"
    argv = ["rsync", "-a", "--delete", *[f"--exclude={e}" for e in excludes]]
    if host == "local":
        argv += [src, f"{target}/"]
    else:
        argv += ["-e", shlex.join(["ssh", *ssh.site.ssh_options]), src, f"{host}:{target}/"]
    if dry_run:
        print(f"dry: mkdir -p {target} on {host}; {shlex.join(argv)}")
        return True
    rc, _, err = ssh.run(host, f"mkdir -p {shlex.quote(str(target))}")
    if rc != 0:
        print(f"{host}: mkdir failed: {err.strip()}")
        return False
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if p.returncode != 0:
        print(f"{host}: rsync rc {p.returncode}: {p.stderr.strip()}")
    return p.returncode == 0


def run_after_hook(site: Site, project: Project, cmd: str, values: dict[str, object]) -> bool:
    """Render `cmd` and run it on the head node in the project directory."""
    text = config.render(cmd, values)
    env = dict(os.environ)
    env.update({k: os.path.expandvars(v) for k, v in site.env.items()})
    p = subprocess.run(["/bin/bash", "-c", text], cwd=project.root, env=env, stdin=subprocess.DEVNULL)
    return p.returncode == 0

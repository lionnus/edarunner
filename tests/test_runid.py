"""runid.py and checkout.py against a copy of examples/local-demo. Every write goes to tmp_path."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from edarunner import config, runid, checkout

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def commit_all(repo: Path, msg: str) -> None:
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", msg, cwd=repo)


@pytest.fixture
def project(tmp_path):
    """The demo project under a path with the /edr/ marker, a repo with two commits and one nested repo."""
    root = tmp_path / "edr" / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    edr = root / "edr.toml"
    text = edr.read_text().replace('nested = []', 'nested = ["nonfree"]')
    text = text.replace('state = "~/.edr/{project}"', f'state = "{tmp_path}/state"')
    edr.write_text(text)
    repo = root / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    shutil.copytree(root / "flow", repo / "flow")
    commit_all(repo, "flow")
    (repo / "README").write_text("demo\n")
    commit_all(repo, "readme")
    nested = repo / "nonfree"
    nested.mkdir()
    git("init", "-q", "-b", "main", cwd=nested)
    (nested / "secret.txt").write_text("42\n")
    commit_all(nested, "nonfree")
    return config.load_project(root)


def listing(root: Path) -> list[tuple[str, int, int]]:
    return sorted((str(p.relative_to(root)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in root.rglob("*"))


def test_stage_head_twice_is_idempotent(project):
    repo = project.source.repo
    head = git("rev-parse", "--short", "HEAD", cwd=repo)
    nested_head = git("rev-parse", "--short", "HEAD", cwd=repo / "nonfree")
    a = checkout.checkout(project, "HEAD")
    assert a == checkout.checkout(project, "HEAD")
    assert a.path == project.source.worktrees / head and a.src == head and not a.dirty
    assert git("rev-parse", "HEAD", cwd=a.path) == git("rev-parse", "HEAD", cwd=repo)
    assert (a.path / "flow" / "flow.sh").exists() and (a.path / "README").exists()
    assert a.nested == {"nonfree": nested_head}
    assert (a.path / "nonfree" / "secret.txt").read_text() == "42\n"
    assert git("rev-parse", "--short", "HEAD", cwd=a.path / "nonfree") == nested_head
    assert runid.src_tag(a.path) == head
    assert checkout.find(project, head) == a.path and checkout.find(project, "HEAD") == a.path
    assert checkout.checkout(project, dirty_dir=repo) == a  # a clean tree pins its commit
    old = checkout.checkout(project, "HEAD~1")
    assert old.path != a.path and not (old.path / "README").exists()
    assert checkout.checkout(project) == a  # source.ref of the demo is HEAD
    with pytest.raises(checkout.CheckoutError, match="not checked out"):
        checkout.find(project, "0000000")


def test_dirty_snapshot_tag_is_stable(project):
    repo = project.source.repo
    head = git("rev-parse", "--short", "HEAD", cwd=repo)
    (repo / "README").write_text("changed\n")
    (repo / "notes.txt").write_text("untracked\n")
    a = checkout.checkout(project, dirty_dir=repo)
    assert a == checkout.checkout(project, dirty_dir=repo) and a.dirty
    assert re.fullmatch(rf"{head}-dirty-[0-9a-f]{{8}}", a.src)
    assert a.path == project.source.worktrees / a.src
    assert (a.path / "README").read_text() == "changed\n"
    assert (a.path / "notes.txt").exists() and not (a.path / ".git").exists()
    assert (a.path / "nonfree" / "secret.txt").exists() and not (a.path / "nonfree" / ".git").exists()
    assert "+changed" in (a.path / "source.diff").read_text()
    meta = json.loads((a.path / "source.json").read_text())
    assert meta["src"] == a.src and meta["base"] == head and meta["nested"] == a.nested and meta["dirty"]
    assert a.nested == {"nonfree": git("rev-parse", "--short", "HEAD", cwd=repo / "nonfree")}
    assert runid.src_tag(a.path) == a.src
    assert checkout.find(project, a.src) == a.path
    (repo / "README").write_text("changed again\n")
    assert checkout.checkout(project, dirty_dir=repo).src != a.src





def test_dry_run_writes_nothing(project, tmp_path, capsys):
    repo = project.source.repo
    a = checkout.checkout(project, "HEAD")
    (repo / "README").write_text("changed\n")
    runid.src_tag(repo)  # git refreshes its index stat cache on the first diff
    before = listing(tmp_path)
    old = checkout.checkout(project, "HEAD~1", dry_run=True)
    dirty = checkout.checkout(project, dirty_dir=repo, dry_run=True)
    assert listing(tmp_path) == before
    assert not old.path.exists() and old.nested == {"nonfree": a.nested["nonfree"]}
    assert "-dirty-" in dirty.src and not dirty.path.exists() and dirty.dirty
    out = capsys.readouterr().out
    assert "worktree add" in out and "git clone" in out and "rsync -a" in out

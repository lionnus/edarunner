"""runid.py, checkout.py, the nested commits a plan records and the lag that brief counts, against a copy of
examples/local-demo. Every write goes to tmp_path."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from edarunner import brief, checkout, config, launch, runid
from edarunner.db import Database
from edarunner.hosts import HostProbe, Ssh
from helpers_driver import DEMO

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
    text = edr.read_text().replace('nested = []', 'nested = ["sub"]')
    text = text.replace('state_dir = "~/.edr/{project}"', f'state_dir = "{tmp_path}/state"')
    edr.write_text(text)
    repo = root / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    shutil.copytree(root / "flow", repo / "flow")
    commit_all(repo, "flow")
    (repo / "README").write_text("demo\n")
    commit_all(repo, "readme")
    nested = repo / "sub"
    nested.mkdir()
    git("init", "-q", "-b", "main", cwd=nested)
    (nested / "secret.txt").write_text("42\n")
    commit_all(nested, "sub")
    return config.load_project(root)


def listing(root: Path) -> list[tuple[str, int, int]]:
    return sorted((str(p.relative_to(root)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in root.rglob("*"))


def short(repo: Path) -> str:
    return git("rev-parse", "--short", "HEAD", cwd=repo)


def test_stage_head_twice_is_idempotent(project):
    repo = project.source.repo
    head = git("rev-parse", "--short", "HEAD", cwd=repo)
    nested_head = git("rev-parse", "--short", "HEAD", cwd=repo / "sub")
    a = checkout.checkout(project, "HEAD")
    assert a == checkout.checkout(project, "HEAD")
    assert a.source == f"{head}-n{nested_head}" and a.path == project.source.worktrees / a.source and not a.dirty
    assert git("rev-parse", "HEAD", cwd=a.path) == git("rev-parse", "HEAD", cwd=repo)
    assert (a.path / ".git").is_dir() and git("describe", "--always", "--dirty", cwd=a.path) == head
    assert (a.path / "flow" / "flow.sh").exists() and (a.path / "README").exists()
    assert a.nested == {"sub": nested_head}
    assert (a.path / "sub" / "secret.txt").read_text() == "42\n"
    assert git("rev-parse", "--short", "HEAD", cwd=a.path / "sub") == nested_head
    assert runid.source_tag(a.path, ["sub"]) == a.source
    # A ref, the commit hash of the tree included, resolves with each nested repository at its HEAD.
    assert checkout.find(project, a.source) == checkout.find(project, head) == checkout.find(project, "HEAD") == a.path
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
    assert re.fullmatch(rf"{head}-n{short(repo / 'sub')}-dirty-[0-9a-f]{{8}}", a.source)
    assert a.path == project.source.worktrees / a.source
    assert (a.path / "README").read_text() == "changed\n"
    assert (a.path / "notes.txt").exists() and (a.path / ".git").is_dir()
    assert (a.path / "sub" / "secret.txt").exists() and (a.path / "sub" / ".git").is_dir()
    # git on the copy sees the change, as it would on a host.
    assert git("rev-parse", "--short", "HEAD", cwd=a.path) == head
    assert git("describe", "--always", "--dirty", cwd=a.path).endswith("-dirty")
    assert "+changed" in (a.path / "source.diff").read_text()
    meta = json.loads((a.path / "source.json").read_text())
    assert meta["source"] == a.source and meta["base"] == head and meta["nested"] == a.nested and meta["dirty"]
    assert a.nested == {"sub": git("rev-parse", "--short", "HEAD", cwd=repo / "sub")}
    assert runid.source_tag(a.path, ["sub"]) == a.source
    assert checkout.find(project, a.source) == a.path
    (repo / "README").write_text("changed again\n")
    assert checkout.checkout(project, dirty_dir=repo).source != a.source


def test_untracked_files_and_nested_edits_count_in_the_tag_and_the_diff(project, tmp_path):
    repo, nested = project.source.repo, project.source.nested
    head, sub_head = git("rev-parse", "--short", "HEAD", cwd=repo), git("rev-parse", "--short", "HEAD", cwd=repo / "sub")
    assert runid.source_tag(repo, nested) == f"{head}-n{sub_head}"  # git lists the nested repository as sub/
    (repo / "notes.txt").write_text("untracked\n")
    untracked = runid.source_tag(repo, nested)
    (repo / "sub" / "more.txt").write_text("new\n")
    (repo / "sub" / "secret.txt").unlink()
    both = runid.source_tag(repo, nested)
    assert untracked.startswith(f"{head}-n{sub_head}-dirty-") and both.startswith(f"{head}-n{sub_head}-dirty-")
    assert both != untracked
    a = checkout.checkout(project, dirty_dir=repo)
    assert a.source == both and a.nested == {"sub": sub_head}
    assert (a.path / "sub" / "more.txt").is_file() and not (a.path / "sub" / "secret.txt").exists()
    # The diff and the metadata outlive the clone, which retire removes with its batch.
    kept = project.data / "sources" / both
    assert (kept / "source.diff").read_text() == (a.path / "source.diff").read_text()
    assert json.loads((kept / "source.json").read_text()) == json.loads((a.path / "source.json").read_text())
    # The diff applied to the base, with the nested repository at its HEAD, gives the tree again.
    clone = tmp_path / "clone"
    git("clone", "-q", str(repo), str(clone), cwd=tmp_path)
    git("clone", "-q", str(repo / "sub"), str(clone / "sub"), cwd=tmp_path)
    git("apply", str(kept / "source.diff"), cwd=clone)
    assert (clone / "notes.txt").read_text() == "untracked\n" and (clone / "sub" / "more.txt").read_text() == "new\n"
    assert not (clone / "sub" / "secret.txt").exists()


def test_parse_tag_round_trips_the_checkout_form():
    for tag, names in (("3f9a2c1", []), ("3f9a2c1-dirty-0badc0de", []), ("3f9a2c1-n8e0d4b2", ["flow"]),
                       ("3f9a2c1-n8e0d4b2-n1c2d3e4-dirty-7b21c0d9", ["flow", "pdk"])):
        assert str(runid.parse_tag(tag, names)) == tag
    assert runid.parse_tag("3f9a2c1-n8e0d4b2-n1c2d3e4-dirty-7b21c0d9", ["flow", "pdk"]) == runid.Tag(
        "3f9a2c1", {"flow": "8e0d4b2", "pdk": "1c2d3e4"}, "7b21c0d9")
    # A tag of another nested list, a tag that edr checkout does not make, and a ref are not tags of this project.
    for text in ("3f9a2c1", "3f9a2c1-n8e0d4b2-n1c2d3e4", "3f9a2c1-dirty", "3f9a2c1-n8e0d4b2-dirty-0bad", "origin/main", ""):
        assert runid.parse_tag(text, ["flow"]) is None


def test_a_nested_commit_gives_a_new_tag_and_a_fresh_clone(project):
    repo = project.source.repo
    a = checkout.checkout(project, "HEAD")
    (repo / "README").write_text("changed\n")
    dirty = checkout.checkout(project, dirty_dir=repo)
    (repo / "sub" / "secret.txt").write_text("43\n")
    commit_all(repo / "sub", "sub 2")
    b, again = checkout.checkout(project, "HEAD"), checkout.checkout(project, dirty_dir=repo)
    head, old, new = short(repo), a.nested["sub"], short(repo / "sub")
    assert b.source == f"{head}-n{new}" != a.source and b.path != a.path and b.nested == {"sub": new}
    assert (b.path / "sub" / "secret.txt").read_text() == "43\n" and (a.path / "sub" / "secret.txt").read_text() == "42\n"
    # The same diff over another nested commit is another snapshot.
    assert dirty.source.startswith(f"{head}-n{old}-dirty-") and again.source.startswith(f"{head}-n{new}-dirty-")
    assert runid.parse_tag(dirty.source, ["sub"]).dirty == runid.parse_tag(again.source, ["sub"]).dirty
    assert again.path != dirty.path and (again.path / "sub" / "secret.txt").read_text() == "43\n"
    assert json.loads((again.path / "source.json").read_text())["nested"] == {"sub": new}
    # A ref follows the nested HEAD; a tag keeps its commits and checks them out again once its clone is gone.
    assert checkout.find(project, "HEAD") == b.path and checkout.ensure(project, "HEAD") is None
    shutil.rmtree(a.path)
    assert checkout.ensure(project, a.source) == a and short(a.path / "sub") == old


def test_the_snapshot_holds_what_the_tag_covers_and_refuses_an_undeclared_repository(project, tmp_path):
    repo = project.source.repo
    (repo / ".gitignore").write_text("*.log\nignored/\n")
    (repo / "run.log").write_text("a log\n")
    for name in ("ignored/dep", "loose"):
        (repo / name).mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=repo / name)
        (repo / name / "dep.txt").write_text("dep\n")
        commit_all(repo / name, name)
    before = listing(tmp_path)
    with pytest.raises(runid.GitError, match="loose is a git repository that \\[source\\] nested does not name; "
                                             "add it to nested, or ignore it in .gitignore"):
        checkout.checkout(project, dirty_dir=repo)
    assert listing(tmp_path) == before
    with pytest.raises(runid.GitError, match="nested names gone, but .*gone is not a git repository"):
        runid.source_tag(repo, ["sub", "gone"])
    shutil.rmtree(repo / "loose")
    a = checkout.checkout(project, dirty_dir=repo)
    assert (a.path / ".gitignore").is_file() and not (a.path / "run.log").exists() and not (a.path / "ignored").exists()
    assert "run.log" not in (a.path / "source.diff").read_text()


def test_brief_counts_the_lag_from_the_commit_of_the_tree(project):
    repo = project.source.repo
    old, sub = git("rev-parse", "--short", "HEAD~1", cwd=repo), short(repo / "sub")
    assert brief._behind(project, f"{old}-n{sub}") == 1 == brief._behind(project, f"{old}-n{sub}-dirty-0badc0de")
    assert brief._behind(project, f"{short(repo)}-n{sub}") == 0 and brief._behind(project, old) is None


def test_the_spec_records_the_nested_commits_of_the_checkout(project, tmp_path):
    a = checkout.checkout(project, "HEAD")
    batch = config.load_batch(project, "demo")
    batch.source = a.source
    probes = {"local": HostProbe("local", 4.0, 8.0, str(tmp_path / "scratch"), 50.0)}
    with Database(tmp_path / "edr.db") as db:
        plans = launch.plan(project, batch, Ssh(project.site), db, date="20260926_1200", probes=probes)
    assert [p.nested for p in plans] == [a.nested, a.nested] and a.nested
    spec = launch.write_spec(project.state_dir, "demo", plans[0], tmp_path / "driver.py", dry_run=True)
    assert plans[0].spec["record"]["nested"] == a.nested and not spec.exists()


def test_snapshot_copies_a_same_size_edit_made_in_the_second_of_the_clone(project, monkeypatch):
    # The edit is 0.2 s into a second and the checkout of the clone 0.7 s; rsync compares whole seconds.
    edit, checked_out = 1_700_000_000_200_000_000, 1_700_000_000_700_000_000
    repo = project.source.repo
    edited = {"README": "demO\n", "sub/secret.txt": "43\n"}
    for rel, text in edited.items():
        (repo / rel).write_text(text)
        os.utime(repo / rel, ns=(edit, edit))
    clone = checkout._clone

    def clone_in_the_same_second(src, dst, commit, dry_run):
        clone(src, dst, commit, dry_run)
        for f in (dst / "README", dst / "secret.txt"):
            if f.exists():
                os.utime(f, ns=(checked_out, checked_out))

    monkeypatch.setattr(checkout, "_clone", clone_in_the_same_second)
    a = checkout.checkout(project, dirty_dir=repo)
    assert {rel: (a.path / rel).read_text() for rel in edited} == edited


def test_snapshot_drops_every_file_the_diff_deletes(project):
    repo = project.source.repo
    (repo / "lib").symlink_to("flow")
    (repo / "doc").mkdir()
    (repo / "doc" / "a.txt").write_text("a\n")
    git("add", "lib", "doc", cwd=repo)
    git("commit", "-q", "-m", "link and doc", cwd=repo)
    (repo / "README").unlink()
    (repo / "lib").unlink()
    git("rm", "-q", "flow/seats.sh", cwd=repo)
    git("mv", "flow/kernel.sh", "flow/moved.sh", cwd=repo)
    git("rm", "-q", "--cached", "flow/flow.sh", cwd=repo)
    (repo / "sub" / "secret.txt").unlink()
    shutil.rmtree(repo / "doc")
    (repo / "doc").write_text("a file now\n")
    a = checkout.checkout(project, dirty_dir=repo)
    assert not [p for p in ("README", "lib", "flow/seats.sh", "flow/kernel.sh", "sub/secret.txt") if os.path.lexists(a.path / p)]
    assert (a.path / "flow" / "moved.sh").is_file() and (a.path / "flow" / "flow.sh").is_file()
    assert (a.path / "doc").read_text() == "a file now\n"
    assert "D README" in [ln.strip() for ln in git("status", "--porcelain", cwd=a.path).splitlines()]


def test_dry_run_writes_nothing(project, tmp_path, capsys):
    repo = project.source.repo
    a = checkout.checkout(project, "HEAD")
    (repo / "README").write_text("changed\n")
    runid.source_tag(repo, ["sub"])  # git refreshes its index stat cache on the first diff
    before = listing(tmp_path)
    old = checkout.checkout(project, "HEAD~1", dry_run=True)
    dirty = checkout.checkout(project, dirty_dir=repo, dry_run=True)
    assert listing(tmp_path) == before
    assert not old.path.exists() and old.nested == {"sub": a.nested["sub"]}
    assert "-dirty-" in dirty.source and not dirty.path.exists() and dirty.dirty
    out = capsys.readouterr().out
    assert "git clone --local --no-checkout" in out and "rsync -a" in out

"""Copy the collect paths of a run into data/results.

Every copy is one `rsync -a` from the host to the same relative path under
`data/results/<run_id>/`. A failure is counted and returned, never raised.
"""

from __future__ import annotations

import posixpath
import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import board
from .config import ConfigError, load_json, placeholders, render, resolve_task
from .db import Database
from .guards import Refuse, assert_run_id
from .hosts import Ssh
from .model import Project, Stage

_TASK_END = ("done", "failed")
# rsync exit codes of a lost or refused connection.
_SSH_RC = {12, 30, 35, 255}
_FORMAT = "--out-format=%i %l %n"


@dataclass
class CollectResult:
    """How many files one collection copied, and what failed."""

    files: int = 0
    failures: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)


def load_spec(project: Project, run: dict) -> dict:
    """The run's spec from the state directory, or {} when it has none (an imported tree)."""
    return load_json(project.state_dir / str(run.get("batch") or "") / f"{run.get('run_id')}.spec.json")


def spec_stages(spec: dict) -> list[str] | None:
    """The stage names a run's spec lists, or None when there is no spec."""
    names = [str(s["name"]) for s in spec.get("stages") or [] if s.get("name")]
    return names or None


def spec_tasks(spec: dict, root: str) -> Iterator[tuple[str, str, str]]:
    """(stage, task id, task directory relative to the run root) of each task of the spec's task groups."""
    base = root.rstrip("/") + "/"
    for stage in spec.get("stages") or []:
        for task in stage.get("tasks") or []:
            d = str(task.get("dir") or "")
            yield str(stage.get("name")), str(task["id"]), d[len(base):] if base != "/" and d.startswith(base) else d


def spec_task_dirs(spec: dict, root: str) -> dict[str, str]:
    """Task id -> task directory relative to the run root, from the spec's task groups."""
    return {task: d for _, task, d in spec_tasks(spec, root)}


def spec_task_fields(spec: dict) -> dict[str, dict[str, str]]:
    """Task id -> the fields the task ran with, from the spec's task groups; a task without them is left out."""
    return {str(t["id"]): t["fields"] for st in spec.get("stages") or [] for t in st.get("tasks") or [] if "fields" in t}


def stage_state(project: Project, heartbeat: dict, only: list[str] | None = None) -> tuple[list[str], str | None]:
    """The finished stages of a run in project order, and the running one (None when terminal).

    `only` limits the stages to those the run's spec lists; the tree's earlier
    stages belong to the run that made them.
    """
    current = heartbeat.get("stage")
    names = [n for n in project.stages if only is None or n in only]
    if not current or current not in names:
        return [], None
    i = names.index(current)
    if not board.is_live(heartbeat):
        return names[: i + 1], None
    return names[:i], current


def collect_run(
    project: Project,
    ssh: Ssh,
    db: Database,
    run: dict,
    heartbeat: dict,
    dry_run: bool = False,
    host: str = "",
) -> CollectResult:
    """Copy log/ and the collect paths of every finished stage and task of `run` into data/results.

    Of a running stage with steps, it copies the numbered directories under its collect paths that
    are below the step of the heartbeat. `host` is where the tree is read, as the backend names it;
    empty means the run's host."""
    spec = load_spec(project, run)
    only = spec_stages(spec)
    c = _Copier(project, ssh, db, run, heartbeat, dry_run, spec_task_dirs(spec, str(run.get("root") or "")), spec.get("vars"),
                host)
    if c.result.failures:
        return c.result
    finished, running = stage_state(project, heartbeat, only)
    tasks = [t for t, e in (heartbeat.get("tasks") or {}).items() if e.get("phase") in _TASK_END]
    paths = ["log/"]
    for name in finished:
        paths += c.render(project.stages[name], project.stages[name].collect, tasks)
    stage, step = project.stages.get(running or ""), heartbeat.get("step")
    if stage and stage.steps and step is not None:
        for entry in c.render(stage, stage.collect, tasks):
            paths += c.final_steps(entry, step)
    c.copy_all(paths, "always")
    return c.result


def collect_on_request(
    project: Project, ssh: Ssh, db: Database, run: dict, name: str, dry_run: bool = False
) -> CollectResult:
    """Copy the `collect_on_request` list `name` of every stage; the artifact class is `name`."""
    spec = load_spec(project, run)
    c = _Copier(project, ssh, db, run, {}, dry_run, spec_task_dirs(spec, str(run.get("root") or "")), spec.get("vars"))
    if c.result.failures:
        return c.result
    c.copy_all(_on_request_paths(project, c, spec, run, name), name)
    return c.result


def restore_on_request(
    project: Project, ssh: Ssh, db: Database, run: dict, name: str, host: str, root: str, dry_run: bool = False,
    job_vars: dict[str, str] | None = None,
) -> CollectResult:
    """Copy the `collect_on_request` list `name` of `run` from data/results/<run_id>/ into <host>:<root>.

    `job_vars` fill `{vars.<name>}` where the spec of `run` lacks them.
    """
    spec = load_spec(project, run)
    c = _Copier(project, ssh, db, {**run, "host": host, "root": root}, {}, dry_run,
                spec_task_dirs(spec, str(run.get("root") or "")), {**(job_vars or {}), **spec.get("vars", {})})
    if c.result.failures:
        return c.result
    for entry in dict.fromkeys(_on_request_paths(project, c, spec, run, name)):
        c.push(entry)
    return c.result


def _on_request_paths(project: Project, c: _Copier, spec: dict, run: dict, name: str) -> list[str]:
    """The rendered `collect_on_request.<name>` entries of the stages the spec ran, for the tasks of the heartbeat
    that started."""
    heartbeat = load_json(project.state_dir / str(run.get("batch") or "") / f"{run.get('run_id')}.json")
    tasks = [t for t, e in (heartbeat.get("tasks") or {}).items() if e.get("phase") not in ("held", "skipped")]
    only = spec_stages(spec)
    paths = [p for stage in project.stages.values() if only is None or stage.name in only
             for p in c.render(stage, stage.collect_on_request.get(name, []), tasks)]
    if not paths and not c.result.failures:
        c.result.failures.append(f"no stage has collect_on_request.{name}")
    return paths


class _Copier:
    def __init__(
        self, project: Project, ssh: Ssh, db: Database, run: dict, heartbeat: dict, dry_run: bool,
        task_dirs: dict[str, str] | None = None, job_vars: dict[str, str] | None = None, host: str = "",
    ) -> None:
        self.project, self.ssh, self.db, self.dry_run = project, ssh, db, dry_run
        self.task_dirs = task_dirs or {}
        self.result = CollectResult()
        scalars = {k: v for k, v in {**heartbeat, **run}.items() if isinstance(v, (str, int, float))}
        self.values = placeholders(project, **scalars, vars=job_vars or {})
        self.run_id = str(self.values.get("run_id", ""))
        self.host = host or str(self.values.get("host", ""))
        self.root = str(self.values.get("root", ""))
        try:
            assert_run_id(self.run_id)
        except Refuse as e:
            self.result.failures.append(str(e))
        if not self.host or not self.root:
            self.result.failures.append(f"{self.run_id}: the run has no host or root")
        self.results = project.data / "results" / self.run_id

    def render(self, stage: Stage, entries: list[str], tasks: list[str]) -> list[str]:
        """Render `entries` with the run values; a task group renders once per task."""
        out = []
        for entry in entries:
            for tid in tasks if stage.is_group else [None]:
                try:
                    out.append(render(entry, self._task_values(stage, tid) if tid else self.values))
                except ConfigError as e:
                    self.result.failures.append(f"{stage.name}: {e}")
        return out

    def _task_values(self, stage: Stage, tid: str) -> dict[str, object]:
        # The spec is the truth for a run: its task directory wins over the task table,
        # which may have changed since the launch.
        values = dict(self.values)
        try:
            task = resolve_task(self.project, tid)
            values.update({f"task.{k}": v for k, v in task.fields.items()})
        except ConfigError:
            if tid not in self.task_dirs:
                raise
        values["task.id"] = tid
        values["task_dir"] = self.task_dirs.get(tid) or render(stage.task_dir, values)
        return values

    def final_steps(self, entry: str, step: int) -> list[str]:
        """The numbered directories under `entry` on the host below `step`, the step that runs now."""
        top = shlex.quote(posixpath.join(self.root, entry))
        rc, out, err = self.ssh.run(self.host, f"find {top} -mindepth 1 -maxdepth 1 -type d 2>/dev/null")
        if rc == 255:
            self.result.failures.append(f"{self.host}: {err.strip() or 'no answer'}")
            return []
        dirs = [p.strip() for p in out.splitlines()]
        return [posixpath.relpath(d, self.root) + "/" for d in dirs
                if (n := posixpath.basename(d)).isdecimal() and int(n) < step]

    def copy_all(self, paths: list[str], klass: str) -> None:
        """Copy every distinct path; an empty entry is a counted failure."""
        for entry in dict.fromkeys(paths):
            if entry:
                self._copy(entry, klass)
            else:
                self.result.failures.append("empty collect entry")

    def _copy(self, entry: str, klass: str) -> None:
        # A path without a trailing slash lands in its parent, so a second copy does not nest it.
        base = entry if entry.endswith("/") else entry[: entry.rfind("/") + 1]
        dest = f"{self.results}/{base}"
        src = posixpath.join(self.root, entry)
        if not self.dry_run:
            Path(dest).mkdir(parents=True, exist_ok=True)
        remote = self.host != "local"
        rc, out, err = self._rsync(f"{self.host}:{shlex.quote(src)}" if remote else src, dest, remote)
        if rc in _SSH_RC and self.project.site.nfs_export:
            try:
                export = render(self.project.site.nfs_export, self.values)
                rc, out, err = self._rsync(posixpath.join(export, entry), dest, False)
            except ConfigError as e:
                err = str(e)
        if rc != 0:
            first = err.strip().splitlines()[:1]
            self.result.failures.append(f"{entry}: rsync rc {rc}: {first[0] if first else ''}")
        for line in out.splitlines():
            code, size, name = line.split(" ", 2)
            if code[1:2] == "f":
                self._record(base + name, int(size), klass)

    def push(self, entry: str) -> None:
        """Copy `entry` from data/results back to the same path under the tree on the host."""
        src = f"{self.results}/{entry}"
        if not Path(src).exists():
            self.result.failures.append(f"{entry}: not in {self.results}")
            return
        dest = posixpath.join(self.root, entry)
        remote = self.host != "local"
        if not self.dry_run:
            rc, _, err = self.ssh.run(self.host, f"mkdir -p {shlex.quote(posixpath.dirname(dest.rstrip('/')))}")
            if rc != 0:
                self.result.failures.append(f"{entry}: mkdir rc {rc}: {err.strip()}")
                return
        rc, out, err = self._rsync(src, f"{self.host}:{shlex.quote(dest)}" if remote else dest, remote)
        if rc != 0:
            first = err.strip().splitlines()[:1]
            self.result.failures.append(f"{entry}: rsync rc {rc}: {first[0] if first else ''}")
        for line in out.splitlines():
            code, _, name = line.split(" ", 2)
            if code[1:2] == "f":
                self.result.files += 1
                self.result.copied.append(name)

    def _rsync(self, src: str, dest: str, remote: bool) -> tuple[int, str, str]:
        site = self.project.site
        argv = ["rsync", "-a", _FORMAT, f"--timeout={site.ssh_timeout_s}"]
        if remote:
            argv += ["-e", shlex.join(["ssh", *site.ssh_options])]
        if self.dry_run:
            argv.append("-n")
        try:
            p = subprocess.run(
                [*argv, src, dest], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace"
            )
        except OSError as e:
            return 255, "", str(e)
        return p.returncode, p.stdout, p.stderr

    def _record(self, path: str, size: int, klass: str) -> None:
        self.result.files += 1
        self.result.copied.append(path)
        if not self.dry_run:
            self.db.add_artifact({"run_id": self.run_id, "path": path, "bytes": size, "class": klass})

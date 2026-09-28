"""Metric extraction from collected files."""

from __future__ import annotations

import csv
import itertools
import json
import re
import time
from pathlib import Path

from . import config
from .config import ConfigError, load_hook
from .model import Metric, Project, Stage, Task

TOP = "<top>"
_NUM = re.compile(r"-?\d+(\.\d*)?([eE][-+]?\d+)?$")


def parse_area_hier(text: str) -> list[dict]:
    """One row per instance of a hierarchical area report: instance, depth, area, local_area, cells.

    The top is `<top>` at depth 0; a child's instance is its path from the top, `/` separated.
    `area` includes the children, `local_area` does not. `cells` is None when the report has no count.
    """
    if "Hierarchical Area Report" in text:
        return _area_openroad(text)
    if "Hierarchical cell" in text:
        return _area_synopsys(text)
    raise ValueError("neither a Synopsys nor an OpenROAD hierarchical area report")


def _area_synopsys(text: str) -> list[dict]:
    # report_area -hierarchy: name, global total, percent, local comb, local noncomb, local black box, design.
    lines = text[text.index("Hierarchical cell"):].splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("---")) + 1
    rows: list[dict] = []
    words: list[str] = []
    for ln in lines[start:]:
        if ln.startswith("---"):
            break
        words += ln.split()
        # A long name can push the numbers onto the next line.
        if len(words) < 6 or not all(_NUM.match(w) for w in words[1:6]):
            continue
        name, nums = words[0], [float(w) for w in words[1:6]]
        words = []
        top = not rows
        rows.append({"instance": TOP if top else name, "depth": 0 if top else name.count("/") + 1,
                     "area": nums[0], "local_area": round(sum(nums[2:5]), 6), "cells": None})
    return rows


def _area_openroad(text: str) -> list[dict]:
    # Hierarchy name indented two spaces a level, then global area x5, global instances x5,
    # local area x5 and local instances x5; each group starts with its total.
    body = text[text.index("Hierarchical Area Report"):].splitlines()
    rows: list[dict] = []
    path: list[str] = []
    started = False
    for ln in body[1:]:
        words = ln.split()
        if len(words) == 21 and all(_NUM.match(w) for w in words[1:]):
            started = True
            depth = (len(ln) - len(ln.lstrip(" "))) // 2
            name = words[0].replace("\\", "")
            path = path[:max(depth - 1, 0)] + ([name] if depth else [])
            rows.append({"instance": "/".join(path) if depth else TOP, "depth": depth, "area": float(words[1]),
                         "local_area": float(words[11]), "cells": int(float(words[6]))})
        elif started and words and not words[0].startswith("-"):
            break
    return rows


def parse_file(metric: Metric, path: Path, project_root: Path,
               values: dict[str, object] | None = None) -> tuple[float, int | None] | None:
    """Parse one value from `path` with the parser of `metric`, and the line of a regex value. None means no row: a
    python hook returned None, or `metric` is optional and the file has no match, row or key for it.

    `values` fills the placeholders of a csv `where`, as they fill `file`.
    """
    if metric.regex:
        return _regex(metric, path.read_text())
    if metric.csv:
        where = {k: config.render(str(v), values or {}) for k, v in (metric.csv.get("where") or {}).items()}
        with path.open(newline="") as fh:
            for rec in csv.DictReader(fh):
                if all(rec.get(k) == v for k, v in where.items()):
                    return float(rec[str(metric.csv["column"])]), None
        return _absent(metric, f"no row matches {where}")
    if metric.json:
        obj = json.loads(path.read_text())
        try:
            for key in metric.json.split("."):
                obj = obj[int(key)] if isinstance(obj, list) else obj[key]
        except (KeyError, IndexError):
            return _absent(metric, f"no key {metric.json}")
        return float(obj), None
    if metric.python:
        fn, _ = load_hook(project_root, metric.python)
        value = fn(path)
        return None if value is None else (float(value), None)
    if metric.area_hier:
        return parse_area_hier(path.read_text(errors="replace"))[0]["area"], None
    raise ValueError(f"metric {metric.name} has no parser")


def _absent(metric: Metric, error: str) -> None:
    """No row for an optional metric, else a failed one."""
    if not metric.optional:
        raise ValueError(error)


def _regex(metric: Metric, text: str) -> tuple[float, int] | None:
    """Group 1 of every match, reduced to one value, with the line that value is on."""
    found: list[tuple[float, int]] = []
    line, pos = 1, 0
    matches = re.finditer(metric.regex, text, re.MULTILINE)
    for m in itertools.islice(matches, 1) if metric.reduce == "first" else matches:
        value = float(m.group(1))
        line, pos = line + text.count("\n", pos, m.start(1)), m.start(1)
        found.append((value, line))
    if not found:
        return _absent(metric, f"no match for {metric.regex!r}")
    if metric.reduce == "sum":
        return sum(v for v, _ in found), found[0][1]
    if metric.reduce in ("min", "max"):
        return (min if metric.reduce == "min" else max)(found, key=lambda f: f[0])
    return found[-1]  # `first` stops after one match


def source_path(source_file: str) -> str:
    """The file of a row's `source_file`, without the `:line` of a regex value."""
    path, _, line = source_file.rpartition(":")
    return path if line.isdigit() else source_file


def defines(project: Project, row: dict) -> bool:
    """Whether a metric of `project` still gives rows like `row`: it exists, reads the row's stage, and a numbered
    step of the row belongs to that stage."""
    metric = project.metrics.get(row["name"])
    return (metric is not None and row["stage"] in metric.stage
            and (row["step"] is None or row["step"] in owned_steps(project).get(row["stage"], range(0))))


def failures(rows: list[dict]) -> dict[str, dict]:
    """{metric name: {"count", "first"}} of the rows that failed; `first` is the error of the first one."""
    out: dict[str, dict] = {}
    for r in rows:
        if r["value"] is None:
            out.setdefault(r["name"], {"count": 0, "first": r["source_file"]})["count"] += 1
    return out


def failure_text(failed: dict[str, dict], sep: str = "; ") -> str:
    """`name: n failed: first error` per metric of `failures`."""
    return sep.join(f"{name}: {f['count']} failed: {f['first']}" for name, f in failed.items())


def extract(project: Project, run: dict, results_dir: Path, tasks: dict[str, Task],
            stages: set[str] | None = None, task_dirs: dict[str, str] | None = None) -> list[dict]:
    """Extract every metric of `run` from its collected files under `results_dir`.

    `stages` limits the work to the stages the run's spec lists, and `task_dirs`
    (task id -> directory relative to the root) comes from that spec.
    """
    task_dirs = task_dirs or {}
    run_dir = Path(results_dir) / run["run_id"]
    now = int(time.time())
    base = {k: v for k, v in run.items() if isinstance(v, (str, int, float))}
    rows: list[dict] = []
    owned = owned_steps(project)
    for metric in project.metrics.values():
        for stage_name in metric.stage:
            if stages is not None and stage_name not in stages:
                continue
            stage = project.stages.get(stage_name)
            group = stage is not None and stage.is_group
            for task in list(tasks.values()) if group else [None]:
                rows += _extract_one(project, metric, stage_name, stage, task, base, run_dir, now,
                                     owned.get(stage_name, range(0)), task_dirs.get(task.id) if task else None)
    return rows


def _extract_one(
    project: Project,
    metric: Metric,
    stage_name: str,
    stage: Stage | None,
    task: Task | None,
    base: dict[str, object],
    run_dir: Path,
    now: int,
    owned: range = range(0),
    task_dir: str | None = None,
) -> list[dict]:
    run_id = str(base["run_id"])
    task_id = task.id if task else ""
    values = dict(base)
    if metric.step is not None:
        values["step"] = "{step}" if metric.step == "*" else metric.step
    try:
        if task is not None:
            values.update({f"task.{k}": v for k, v in task.fields.items()})
            values["task_dir"] = task_dir or config.render(stage.task_dir if stage else "", values)
        pattern = config.render(metric.file, values)
    except ConfigError as e:
        text = str(e)
        return [_row(run_id, stage_name, None, task_id, metric, None, text, now)]
    rows = []
    for step, path in _files(pattern, run_dir, metric.step):
        if step is not None and step not in owned:
            continue  # a numbered step belongs to one stage; the others skip it
        rel = str(path.relative_to(run_dir))
        instances = None
        try:
            if metric.area_hier:
                instances = parse_area_hier(path.read_text(errors="replace"))
                value, source = instances[0]["area"], rel
                instances = [i for i in instances if i["depth"] <= metric.area_hier]
            else:
                got = parse_file(metric, path, project.root, values if step is None else {**values, "step": step})
                if got is None:
                    continue
                value, line = got
                source = rel if line is None else f"{rel}:{line}"
        except Exception as e:  # a parse error is a row, never a crash
            value, source = None, f"{rel}: {e}"
        row = _row(run_id, stage_name, step, task_id, metric, value, source, now)
        if instances:
            row["instances"] = instances
        rows.append(row)
    return rows


def step_totals(project: Project) -> dict[str, int]:
    """The step count of the flow at the end of each stage that has `steps`."""
    return {k: r.stop for k, r in owned_steps(project).items()}


def owned_steps(project: Project) -> dict[str, range]:
    """The step numbers each stage owns, in stage order.

    A `steps` list is indexed by the step number and continues the previous stage's
    list. A list that is not longer than the steps before it names this stage's own
    steps only, so it continues from the previous end. A stage without `steps`
    owns no numbered step.
    """
    owned: dict[str, range] = {}
    lo = 0
    for name, stage in project.stages.items():
        if not stage.steps:
            continue
        hi = len(stage.steps)
        if hi <= lo:
            hi = lo + len(stage.steps)
        owned[name] = range(lo, hi)
        lo = hi
    return owned


def _files(pattern: str, run_dir: Path, step: str | None) -> list[tuple[int | None, Path]]:
    if step != "*":
        path = run_dir / pattern
        return [(None if step is None else int(step), path)] if path.is_file() else []
    rx = re.compile(re.escape(pattern).replace(re.escape("{step}"), r"(\d+)"))
    found = []
    for path in run_dir.glob(pattern.replace("{step}", "*")):
        m = rx.fullmatch(str(path.relative_to(run_dir)))
        if m and path.is_file():
            found.append((int(m.group(1)), path))
    return sorted(found)




def _row(
    run_id: str,
    stage: str,
    step: int | None,
    task: str,
    metric: Metric,
    value: float | None,
    source: str,
    now: int,
) -> dict:
    return {
        "run_id": run_id,
        "stage": stage,
        "step": step,
        "task": task,
        "name": metric.name,
        "canonical": metric.canonical,
        "value": value,
        "unit": metric.unit,
        "source_file": source,
        "extracted_at": now,
    }

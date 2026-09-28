"""Metric extraction from collected files."""

from __future__ import annotations

import csv
import itertools
import json
import re
import time
from fnmatch import fnmatchcase
from pathlib import Path

from . import config
from .config import ConfigError, load_hook
from .model import Metric, Parameter, Project, Stage, Task

TOP = "<top>"
_NUM = re.compile(r"-?\d+(\.\d*)?([eE][-+]?\d+)?$")


def parse_area_hier(text: str) -> list[dict]:
    """One row per instance of a hierarchical area report: instance, depth, part, value, local, cells.

    The top is `<top>` at depth 0; a child's instance is its path from the top, `/` separated. `value` is the area
    with the children, `local` the area without them, and `part` is empty. `cells` is None when the report has no
    count.
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
        rows.append({"instance": TOP if top else name, "depth": 0 if top else name.count("/") + 1, "part": "",
                     "value": nums[0], "local": round(sum(nums[2:5]), 6), "cells": None})
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
            rows.append({"instance": "/".join(path) if depth else TOP, "depth": depth, "part": "", "value": float(words[1]),
                         "local": float(words[11]), "cells": int(float(words[6]))})
        elif started and words and not words[0].startswith("-"):
            break
    return rows


def parse_instances(metric: Metric, path: Path, keep: bool = True) -> tuple[tuple[float, int | None] | None, list[dict]]:
    """The value of an `area_hier` or `table` metric with the line of the `top` row of a table, and the instance
    rows of the file, as `parse_area_hier` gives them, each value times `scale`. `keep` keeps the rows the metric
    stores: of a hierarchy down to its depth, of a table those that `where` matches down to `max_depth`; without it,
    every row. The value is None when the metric is optional and no row matches `top`.
    """
    f = metric.scale
    if metric.area_hier:
        rows = [{**r, "value": r["value"] * f, "local": r["local"] * f} for r in parse_area_hier(path.read_text(errors="replace"))]
        return (rows[0]["value"], None), [r for r in rows if not keep or r["depth"] <= metric.area_hier]
    t = metric.table or {}
    where, top, cols = t.get("where") or {}, t["top"], {k: str(t[k]) for k in ("instance", "value", "depth", "part", "local") if k in t}
    got, rows, seen = None, [], set()
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        lack = sorted({*cols.values(), *where, *top} - set(reader.fieldnames or []))
        if lack:
            raise ValueError(f"no column {', '.join(lack)}")
        for rec in reader:
            inst = rec[cols["instance"]]
            row = {"instance": inst, "depth": int(rec[cols["depth"]]) if "depth" in cols else inst.count("/"),
                   "part": rec[cols["part"]] if "part" in cols else "", "value": float(rec[cols["value"]]) * f,
                   "local": float(rec[cols["local"]]) * f if "local" in cols else None, "cells": None}
            if got is None and _match(rec, top):
                got = row["value"], reader.line_num
            if keep and not (_match(rec, where) and row["depth"] <= t.get("max_depth", row["depth"])):
                continue
            if (row["part"], inst) in seen:
                raise ValueError(f"instance {inst} repeats in part '{row['part']}'; name a column of unique paths")
            seen.add((row["part"], inst))
            rows.append(row)
    return got or _absent(metric, f"no row matches top {top}"), rows


def _match(rec: dict, where: dict) -> bool:
    """Whether the value of each column of `where` matches its glob or one of its list of globs."""
    return all(any(fnmatchcase(rec.get(k) or "", str(g)) for g in (v if isinstance(v, list) else [v])) for k, v in where.items())


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


def extract(project: Project, run: dict, results_dir: Path, tasks: dict[tuple[str, str], Task],
            stages: set[str] | None = None, task_dirs: dict[tuple[str, str], str] | None = None) -> list[dict]:
    """Extract every metric of `run` from its collected files under `results_dir`.

    `tasks` holds the tasks by (stage, task id), and a task group reads its own. `stages` limits the work to the
    stages the run's spec lists, and `task_dirs` ((stage, task id) -> directory relative to the root) comes from
    that spec.
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
            for task in [t for (s, _), t in tasks.items() if s == stage_name] if group else [None]:
                rows += _extract_one(project, metric, stage_name, stage, task, base, run_dir, now,
                                     owned.get(stage_name, range(0)), task_dirs.get((stage_name, task.id)) if task else None)
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
    for step, path in find_files(pattern, run_dir, metric.step):
        if step is not None and step not in owned:
            continue  # a numbered step belongs to one stage; the others skip it
        rel = str(path.relative_to(run_dir))
        instances = None
        try:
            if metric.area_hier or metric.table:
                got, instances = parse_instances(metric, path)
            else:
                got = parse_file(metric, path, project.root, values if step is None else {**values, "step": step})
                got = got and (got[0] * metric.scale, got[1])
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


def parse_parameters(param: Parameter, path: Path, project_root: Path) -> dict[str, str]:
    """The keys and values that one parameter table reads from `path`, each value as text."""
    if param.python:
        fn, _ = load_hook(project_root, param.python)
        got = fn(path) or {}
    elif param.regex:
        with path.open("rb") as fh:
            text = fh.read(param.head_bytes or -1).decode(errors="replace")
        rx = re.compile(param.regex, re.MULTILINE)
        named, got = "key" in rx.groupindex, {}
        for m in rx.finditer(text):
            got.setdefault(m["key"] if named else param.name, m["value"] if named else m.group(1))
        if not got:
            raise ValueError(f"no match for {param.regex!r}" + (f" in the first {param.head_bytes} bytes" if param.head_bytes else ""))
    else:
        got = json.loads(path.read_text())
        try:
            for key in filter(None, param.json.split(".")):
                got = got[key]
        except (KeyError, TypeError):
            raise ValueError(f"no key {param.json}") from None
    if not isinstance(got, dict):
        raise ValueError(f"a {type(got).__name__}, not an object of keys and values")
    return {str(k): v if isinstance(v, str) else json.dumps(v) for k, v in got.items()}


def extract_parameters(project: Project, run: dict, results_dir: Path,
                       stages: list[str] | None = None) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """The parameters that the collected files of `run` give, and a flag `parameters.<name>` (task, check, text) for
    each table whose file did not parse. `stages` limits the tables to the stages of the run's spec."""
    run_dir = Path(results_dir) / run["run_id"]
    values = {k: v for k, v in run.items() if isinstance(v, (str, int, float))}
    got: dict[str, str] = {}
    failed = []
    for p in project.parameters.values():
        if stages is not None and p.stage not in stages:
            continue
        rel = p.file
        try:
            rel = config.render(p.file, values)
            if (run_dir / rel).is_file():
                for k, v in parse_parameters(p, run_dir / rel, project.root).items():
                    got.setdefault(k, v)
        except Exception as e:  # a parse error is a flag, never a crash
            failed.append(("", f"parameters.{p.name}", f"{rel}: {e}"))
    return got, failed


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


def find_files(pattern: str, run_dir: Path, step: str | None) -> list[tuple[int | None, Path]]:
    """The files of `pattern` under `run_dir`, each with its step, in step order. With `step = "*"`, `{step}` matches
    a step number; `*` matches any part of a name. Of several files for one step, the first by name counts."""
    fixed = None if step in (None, "*") else int(step)
    if step != "*" and "*" not in pattern:
        path = run_dir / pattern
        return [(fixed, path)] if path.is_file() else []
    rx = re.compile(re.escape(pattern).replace(re.escape("{step}"), r"(\d+)").replace(r"\*", "[^/]*"))
    found: dict[int | None, Path] = {}
    for path in sorted(run_dir.glob(pattern.replace("{step}", "*"))):
        m = rx.fullmatch(str(path.relative_to(run_dir)))
        if m and path.is_file():
            found.setdefault(int(m.group(1)) if step == "*" else fixed, path)
    return sorted(found.items())


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

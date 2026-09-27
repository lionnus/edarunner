"""Metric extraction from collected files."""

from __future__ import annotations

import ast
import csv
import json
import operator
import re
import time
from collections.abc import Callable
from pathlib import Path

from .config import load_hook
from .model import Metric, Project, Stage, Task

_PLACEHOLDER = re.compile(r"\{([\w.]+)\}")
_OPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def evaluate(expr: str, names: dict[str, float]) -> float:
    """Evaluate `expr` over `names`; only numbers, names and + - * / are allowed."""

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return float(names[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise ValueError(f"{type(node).__name__} is not allowed in a metric expr")

    return ev(ast.parse(expr, mode="eval"))


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


def parse_file(metric: Metric, path: Path, project_root: Path) -> float:
    """Parse one value from `path` with the parser of `metric`."""
    if metric.regex:
        m = re.search(metric.regex, path.read_text(), re.MULTILINE)
        if not m:
            raise ValueError(f"no match for {metric.regex!r}")
        return float(m.group(1))
    if metric.csv:
        where = metric.csv.get("where") or {}
        with path.open(newline="") as fh:
            for rec in csv.DictReader(fh):
                if all(rec.get(k) == str(v) for k, v in where.items()):
                    return float(rec[str(metric.csv["column"])])
        raise ValueError(f"no row matches {where}")
    if metric.json:
        obj = json.loads(path.read_text())
        for key in metric.json.split("."):
            obj = obj[int(key)] if isinstance(obj, list) else obj[key]
        return float(obj)
    if metric.python:
        fn, _ = load_hook(project_root, metric.python)
        return float(fn(path))
    if metric.area_hier:
        return parse_area_hier(path.read_text(errors="replace"))[0]["area"]
    raise ValueError(f"metric {metric.name} has no parser")


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
    owned = _owned_steps(project)
    for metric in project.metrics.values():
        if metric.expr:
            continue
        for stage_name in metric.stage:
            if stages is not None and stage_name not in stages:
                continue
            stage = project.stages.get(stage_name)
            group = stage is not None and stage.is_group
            for task in list(tasks.values()) if group else [None]:
                rows += _extract_one(project, metric, stage_name, stage, task, base, run_dir, now,
                                     owned.get(stage_name, range(0)), task_dirs.get(task.id) if task else None)
    rows += _expressions(project, rows, run["run_id"], now)
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
            values["task_dir"] = task_dir or _render(stage.task_dir if stage else "", values)
        pattern = _render(metric.file, values)
    except KeyError as e:
        text = f"{metric.file}: no value for placeholder {e}"
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
                value, source = parse_file(metric, path, project.root), rel
        except Exception as e:  # a parse error is a row, never a crash
            value, source = None, f"{rel}: {e}"
        row = _row(run_id, stage_name, step, task_id, metric, value, source, now)
        if instances:
            row["instances"] = instances
        rows.append(row)
    return rows


def _expressions(project: Project, rows: list[dict], run_id: str, now: int) -> list[dict]:
    groups: dict[tuple, dict[str, float]] = {}
    for r in rows:
        if r["value"] is not None:
            groups.setdefault((r["stage"], r["step"], r["task"]), {})[r["name"]] = r["value"]
    out = []
    for metric in project.metrics.values():
        if not metric.expr:
            continue
        for (stage, step, task), names in groups.items():
            if metric.stage and stage not in metric.stage:
                continue
            try:
                value, source = evaluate(metric.expr, names), metric.expr
                names[metric.name] = value
            except KeyError:
                continue  # an input metric has not arrived yet
            except Exception as e:
                value, source = None, f"{metric.expr}: {e}"
            out.append(_row(run_id, stage, step, task, metric, value, source, now))
    return out


def step_totals(project: Project) -> dict[str, int]:
    """The step count of the flow at the end of each stage that has `steps`."""
    return {k: r.stop for k, r in _owned_steps(project).items()}


def _owned_steps(project: Project) -> dict[str, range]:
    """The step numbers each stage owns, in stage order.

    A `steps` list is indexed by the step number and continues the previous stage's
    list. A list that is shorter than the steps before it names this stage's own
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


def _render(text: str, values: dict[str, object]) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)


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

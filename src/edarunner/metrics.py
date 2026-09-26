"""Metric extraction from collected files, and the FlexLM probe parser.

See docs/design.md section 3.1 (metrics) and section 3.2 (the probe line).
"""

from __future__ import annotations

import ast
import csv
import importlib.util
import json
import operator
import re
import time
from collections.abc import Callable
from pathlib import Path

from .model import Metric, Project, Stage, Task

_PLACEHOLDER = re.compile(r"\{([\w.]+)\}")
_FLEXLM = r"Users of {}:\s*\(Total of (\d+) licenses? issued;\s*Total of (\d+) licenses? in use\)"
_OPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def parse_flexlm(text: str, feature: str) -> tuple[int, int] | None:
    """Return (issued, used) of `feature` from lmstat output, or None when the line is absent."""
    m = re.search(_FLEXLM.format(re.escape(feature)), text)
    return (int(m.group(1)), int(m.group(2))) if m else None


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
        file, func = metric.python.removeprefix("python:").rsplit(":", 1)
        return float(_hook(project_root / file, func)(path))
    raise ValueError(f"metric {metric.name} has no parser")


def extract(project: Project, run: dict, results_dir: Path, tasks: dict[str, Task]) -> list[dict]:
    """Extract every metric of `run` from its collected files under `results_dir`."""
    run_dir = Path(results_dir) / run["run_id"]
    now = int(time.time())
    base = {k: v for k, v in run.items() if isinstance(v, (str, int, float))}
    rows: list[dict] = []
    for metric in project.metrics.values():
        if metric.expr:
            continue
        for stage_name in metric.stage:
            stage = project.stages.get(stage_name)
            group = stage is not None and stage.is_group
            for task in list(tasks.values()) if group else [None]:
                rows += _extract_one(project, metric, stage_name, stage, task, base, run_dir, now)
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
) -> list[dict]:
    run_id = str(base["run_id"])
    task_id = task.id if task else ""
    values = dict(base)
    if metric.step is not None:
        values["step"] = "{step}" if metric.step == "*" else metric.step
    try:
        if task is not None:
            values.update({f"task.{k}": v for k, v in task.fields.items()})
            values["task_dir"] = _render(stage.task_dir if stage else "", values)
        pattern = _render(metric.file, values)
    except KeyError as e:
        text = f"{metric.file}: no value for placeholder {e}"
        return [_row(run_id, stage_name, None, task_id, metric, None, text, now)]
    rows = []
    for step, path in _files(pattern, run_dir, metric.step):
        rel = str(path.relative_to(run_dir))
        try:
            value, source = parse_file(metric, path, project.root), rel
        except Exception as e:  # a parse error is a row, never a crash
            value, source = None, f"{rel}: {e}"
        rows.append(_row(run_id, stage_name, step, task_id, metric, value, source, now))
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


def _hook(file: Path, func: str) -> Callable[[Path], object]:
    spec = importlib.util.spec_from_file_location(file.stem, file)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, func)


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

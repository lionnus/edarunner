"""Views over the run database: area deltas, metrics per step, runtimes, host and run samples.

Each view returns plain rows for --json and a rich renderable for a person. A row
keeps the source of its number: a file under data/results, or the table it came from.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text

from . import board
from .db import Database
from .model import Project

Row = dict[str, Any]


def names(runs: list[Row]) -> dict[str, str]:
    """A column name per run: the label, or label@src when two runs share a label."""
    labels = [r.get("label") for r in runs]
    return {r["run_id"]: str(r.get("label")) if labels.count(r.get("label")) == 1 else f"{r.get('label')}@{r.get('src')}"
            for r in runs}


def _num(v: float | None, digits: int = 1) -> str | None:
    return None if v is None else f"{v:.{digits}f}"


def _pct(v: float | None, base: float | None) -> str | None:
    # A percent across a sign change, such as a slack from +1 ps to -1 ps, says nothing.
    return None if v is None or not base or v * base < 0 else f"{(v - base) / abs(base) * 100:+.1f}%"


# hierarchical area

def _step_key(r: Row) -> tuple:
    return (-1 if r["step"] is None else r["step"], r["stage"], r["name"])


def area_delta(db: Database, runs: list[Row], depth: int, instance: str | None = None,
               stage: str | None = None, step: int | None = None) -> tuple[list[Row], list[Row]]:
    """(the report of each run, one row per instance at `depth`) with the area of each run and the delta to the first.

    Each run is compared at the last step with an area report that every run has, else at its own last one.
    """
    tops = {r["run_id"]: {_step_key(a): a for a in db.area(run_ids=[r["run_id"]], stage=stage, step=step, depth=0)}
            for r in runs}
    common = set.intersection(*(set(t) for t in tops.values())) if tops else set()
    picked = []
    for r in runs:
        have = tops[r["run_id"]]
        if not have:
            continue
        top = have[max(common) if common else max(have)]
        picked.append({"run_id": r["run_id"], "label": r.get("label"), "src": r.get("src"), "stage": top["stage"],
                       "step": top["step"], "name": top["name"], "area": top["area"], "unit": top.get("unit"),
                       "source_file": top.get("source_file")})
    table: dict[str, Row] = {}
    for p in picked:
        for a in db.area(run_ids=[p["run_id"]], stage=p["stage"], step=p["step"], instance=instance, depth=depth):
            if a["name"] == p["name"]:
                table.setdefault(a["instance"], {"instance": a["instance"], "area": {}})["area"][p["run_id"]] = a["area"]
    first = picked[0]["run_id"] if picked else None
    rows = sorted(table.values(), key=lambda t: -(t["area"].get(first) or 0.0))
    for t in rows:
        base = t["area"].get(first)
        t["delta"] = {rid: None if v is None or base is None else round(v - base, 6)
                      for rid, v in ((p["run_id"], t["area"].get(p["run_id"])) for p in picked[1:])}
    return picked, rows


def area_view(picked: list[Row], rows: list[Row], depth: int) -> RenderableType:
    """Instance, one area column per run, and the delta of each run to the first; the sources below."""
    if not picked or not rows:
        return "no area rows"
    col = names(picked)
    ids = [p["run_id"] for p in picked]
    head = ["instance", *[col[i] for i in ids]]
    for i in ids[1:]:
        head += [f"Δ {col[i]}", "Δ %"]
    total = {p["run_id"]: p["area"] for p in picked}
    body = []
    for t in [*rows, {"instance": "<top>", "area": total}]:
        a = t["area"]
        line = [t["instance"], *[_num(a.get(i)) for i in ids]]
        for i in ids[1:]:
            d = None if a.get(i) is None or a.get(ids[0]) is None else a[i] - a[ids[0]]
            line += [_num(d), _pct(a.get(i), a.get(ids[0]))]
        body.append(line)
    right = tuple(h for h in head if h != "instance")
    src = Text("\n".join(f"{col[p['run_id']]}: {p['stage']} step {'-' if p['step'] is None else p['step']}, "
                         f"{p['source_file']}" for p in picked), style="dim")
    unit = next((p["unit"] for p in picked if p.get("unit")), "")
    return Group(Text(f"area {unit} at depth {depth}".replace("  ", " "), style="bold"), board.table(head, body, right=right), src)


# metrics per step

def step_name(project: Project | None, stage: str, step: int | None) -> str | None:
    """The name of a step number from the stage's `steps` list."""
    steps = project.stages[stage].steps if project and stage in project.stages else []
    return steps[step] if step is not None and 0 <= step < len(steps) else None


def _fmt(v: float | None) -> str | None:
    return None if v is None else f"{v:.6g}"


def side_by_side(project: Project | None, runs: list[Row], rows: list[Row]) -> list[Row]:
    """One row per stage, step, task and metric, with the value of each run and the delta of each to the first."""
    order = {n: i for i, n in enumerate(project.stages)} if project else {}
    ids = [r["run_id"] for r in runs]
    out: dict[tuple, Row] = {}
    for m in rows:
        key = (m["stage"], m.get("step"), m.get("task") or "", m["name"])
        row = out.setdefault(key, {"stage": m["stage"], "step": m.get("step"),
                                   "step_name": step_name(project, m["stage"], m.get("step")),
                                   "task": m.get("task") or "", "metric": m["name"], "unit": m.get("unit"),
                                   "value": {}, "source_file": {}})
        row["value"][m["run_id"]] = m["value"]
        row["source_file"][m["run_id"]] = m.get("source_file")
    for row in out.values():
        base = row["value"].get(ids[0])
        row["delta"] = {i: None if row["value"].get(i) is None or base is None else round(row["value"][i] - base, 9)
                        for i in ids[1:]}
    return sorted(out.values(), key=lambda r: (order.get(r["stage"], len(order)), -1 if r["step"] is None else r["step"],
                                               r["task"], r["metric"]))


def side_by_side_view(runs: list[Row], rows: list[Row]) -> RenderableType:
    """stage, step, name, task, metric, one column per run, and the percent of each run to the first."""
    if not rows:
        return "no metrics"
    col = names(runs)
    ids = [r["run_id"] for r in runs]
    head = ["stage", "step", "name", "task", "metric", *[col[i] for i in ids]]
    for i in ids[1:]:
        head += [f"Δ {col[i]}", "Δ %"]
    body = []
    for r in rows:
        line = [r["stage"], r["step"], r["step_name"], r["task"], r["metric"], *[_fmt(r["value"].get(i)) for i in ids]]
        for i in ids[1:]:
            line += [_fmt(r["delta"][i]), _pct(r["value"].get(i), r["value"].get(ids[0]))]
        body.append(line)
    right = ("step", *head[5:])
    return board.table(head, body, styles={"metric": "bold"}, right=right)


def over_steps(project: Project | None, rows: list[Row]) -> list[Row]:
    """One row per step of one run: stage, step, step name, and each metric with a step."""
    out: dict[tuple, Row] = {}
    for m in rows:
        if m.get("step") is None or m.get("task"):
            continue
        row = out.setdefault((m["step"], m["stage"]), {"stage": m["stage"], "step": m["step"],
                                                       "step_name": step_name(project, m["stage"], m["step"]),
                                                       "value": {}, "source_file": {}})
        row["value"][m["name"]] = m["value"]
        row["source_file"][m["name"]] = m.get("source_file")
    return [out[k] for k in sorted(out)]


def over_steps_view(rows: list[Row]) -> RenderableType:
    """A column per metric; with one metric, its change from the step before and its source file."""
    if not rows:
        return "no metric with a step"
    keys = sorted({k for r in rows for k in r["value"]})
    head = ["stage", "step", "name", *keys]
    body = [[r["stage"], r["step"], r["step_name"], *[_fmt(r["value"].get(k)) for k in keys]] for r in rows]
    if len(keys) == 1:
        head += ["Δ", "source"]
        prev = None
        for line, r in zip(body, rows):
            v = r["value"].get(keys[0])
            line += [None if v is None or prev is None else _fmt(v - prev), r["source_file"].get(keys[0])]
            prev = v if v is not None else prev
    return board.table(head, body, styles={"source": "dim"}, right=("step", *keys, "Δ"))

"""Views over the project database: instances side by side, metrics per step, runtimes, host and run samples.

Each view returns plain rows for --json and a rich renderable for a person. A row
keeps the source of its number: a file under data/results, or the table it came from.
"""

from __future__ import annotations

import bisect
import re
import time
from collections import Counter
from fnmatch import fnmatchcase
from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text

from . import board, config
from .config import ConfigError
from .db import Database
from .guards import Refuse
from .metrics import find_files, owned_steps, parse_instances, source_path
from .model import Project

Row = dict[str, Any]


def names(runs: list[Row]) -> dict[str, str]:
    """A name per run: the label, or label@source when the runs come from more than one source; a prefix of the run
    id follows when two runs share that name."""
    mixed = len({r.get("source") for r in runs}) > 1
    base = {r["run_id"]: f"{r.get('label')}@{r.get('source')}" if mixed else str(r.get("label")) for r in runs}
    twins = Counter(base.values())
    return {i: n if twins[n] == 1 else f"{n} {board.prefix(i, list(base))}" for i, n in base.items()}


def _pct(v: float | None, base: float | None) -> str | None:
    # A percent across a sign change, such as a slack from +1 ps to -1 ps, says nothing.
    return None if v is None or not base or v * base < 0 else f"{(v - base) / abs(base) * 100:+.1f}%"


# the step of record

def mark_record(project: Project | None, rows: list[Row]) -> list[Row]:
    """Set `record` on each metric row: 1 at the step of record of its run, task and metric, 0 on the other rows of a
    metric with `record`, None for a metric without it.

    The step of record is the deepest step of the record stage with a value, at or after `from`. Only a step that
    the stage owns counts, so an export step never stands in for a pnr step.
    """
    owned = owned_steps(project) if project else {}
    best: dict[tuple, Row] = {}
    for m in rows:
        metric = project.metrics.get(m["name"]) if project else None
        rec = metric.record if metric else None
        m["record"] = None if rec is None else 0
        if (rec and m["stage"] == rec["stage"] and m.get("value") is not None
                and m.get("step") in owned.get(m["stage"], ()) and m["step"] >= rec.get("from", 0)):
            key = (m["run_id"], m.get("task") or "", m["name"])
            if key not in best or m["step"] > best[key]["step"]:
                best[key] = m
    for m in best.values():
        m["record"] = 1
    return rows


def _depth(project: Project | None, m: Row) -> tuple:
    # A row without a step number sorts first; for those, a later stage counts as deeper.
    order = list(project.stages) if project else []
    return -1 if m.get("step") is None else m["step"], order.index(m["stage"]) if m["stage"] in order else len(order)


def pick_step(project: Project | None, runs: list[Row], rows: list[Row], stage: str | None = None,
              step: int | None = None) -> tuple[dict[str, Row], list[Row]]:
    """The row each run shows for one metric and task, by run id, and the runs that lack it.

    With `step`, each run is at that step. For a metric with `record`, unless `stage` names another stage, each run
    is at its step of record. Else each run is at the deepest step that every run with a value has, or at its own
    deepest step when they share none. A run without the step of record or without `step` is missing, with its
    steps of that stage.
    """
    have: dict[str, list[Row]] = {}
    for m in rows:
        if m.get("value") is not None:
            have.setdefault(m["run_id"], []).append(m)
    if not have:
        return {}, []
    metric = project.metrics.get(rows[0]["name"]) if project else None
    rec = metric.record if metric and metric.record and stage in (None, metric.record["stage"]) else None
    if step is None and rec is None:
        common = set.intersection(*({_depth(project, m) for m in ms} for ms in have.values()))
        at = max(common) if common else None
        return {rid: max((m for m in ms if at is None or _depth(project, m) == at), key=lambda m: _depth(project, m))
                for rid, ms in have.items()}, []
    if step is not None:
        chosen = {m["run_id"]: m for ms in have.values() for m in ms if m.get("step") == step}
        if not chosen:
            return {}, []
        where = next(iter(chosen.values()))["stage"]
    else:
        chosen = {m["run_id"]: m for m in mark_record(project, rows) if m["record"] == 1}
        where = rec["stage"]
    col = names(runs)
    return chosen, [{"run_id": r["run_id"], "label": col[r["run_id"]], "metric": rows[0]["name"],
                     "task": rows[0].get("task") or "", "stage": where,
                     "steps": sorted({m["step"] for m in have.get(r["run_id"], [])
                                      if m["stage"] == where and m.get("step") is not None})}
                    for r in runs if r["run_id"] not in chosen]


def _at(stage: str, step: int | None) -> str:
    return "" if step is None else f" ({stage} {step})"


def missing_lines(missing: list[Row]) -> list[str]:
    """One line per run and stage that lacks the step of record or the step asked for, with the steps it has."""
    def steps(stage: str, have: list[int]) -> str:
        if len(have) < 2:
            return f"{stage} step {have[0]}" if have else f"no {stage} step"
        span = have == list(range(have[0], have[-1] + 1))
        return f"{stage} steps " + (f"{have[0]} to {have[-1]}" if span else ", ".join(map(str, have)))
    return list(dict.fromkeys(f"missing: {m['label']} has {steps(m['stage'], m['steps'])}" for m in missing))


def _with_missing(view: RenderableType, missing: list[Row]) -> RenderableType:
    lines = missing_lines(missing)
    return Group(view, Text("\n".join(lines), style="bold")) if lines else view


# instances

GE = {"kGE": 1e3, "MGE": 1e6}


def to_ge(project: Project, unit: str | None, rows: list[Row], keys: tuple[str, ...] = ("value",)) -> list[Row]:
    """`rows` with the numbers under `keys` of each row in um2, a number or a dict by run, in `unit`, kGE or MGE, by
    the project's `ge_um2`; such a row takes the unit. Without `unit`, `rows` as they are."""
    if unit is None:
        return rows
    if not project.ge_um2:
        raise Refuse(f"--unit {unit} needs ge_um2 in edr.toml, the area of one gate equivalent in um2")
    f = 1 / (project.ge_um2 * GE[unit])
    for r in rows:
        if r.get("unit") == "um2":
            for k in set(keys) & set(r):
                v = r[k]
                r[k] = {i: x if x is None else x * f for i, x in v.items()} if isinstance(v, dict) else v if v is None else v * f
            r["unit"] = unit
    return rows


def _few(items: list[str]) -> str:
    return ", ".join(items[:8]) + (f" and {len(items) - 8} more" if len(items) > 8 else "")


def instance_delta(project: Project, db: Database, runs: list[Row], name: str, task: str = "", part: str | None = None,
                   depth: int = 1, instance: str | None = None, stage: str | None = None, step: int | None = None) -> Row:
    """The instances of metric `name` side by side: each run at the step that `pick_step` takes for the metric and
    `task`, and one row per instance of `part` at `depth` whose path matches the glob `instance`, with the value of
    each run and the delta to the first. A run without the instance counts as 0. Then come `<sum>` of those rows,
    `<other>`, the top less that sum, and `<top>`, the rows at the lowest depth of the part; at depth 0 only `<top>`.

    Without `part`, the part is the one that the metric's `top` names, else none. A depth deeper than the rows in the
    database is read from the file that the metric row of the run cites. A task that no run has, a run without a
    value and a part that a run lacks are refused.
    """
    metric, col = project.metrics.get(name), names(runs)
    mets = [m for m in db.metrics(run_ids=[r["run_id"] for r in runs], stage=stage, name=name) if m["name"] == name]
    tasks = sorted({m["task"] for m in mets})
    if task not in tasks:
        raise Refuse(f"{name} has no rows of task '{task}'; name one with --task: {_few(tasks)}")
    mets = [m for m in mets if m["task"] == task]
    lack = [col[r["run_id"]] for r in runs if r["run_id"] not in {m["run_id"] for m in mets if m["value"] is not None}]
    if lack:
        raise Refuse(f"no {name} value" + (f" of task {task}" if task else "") + f" in {', '.join(lack)}")
    if part is None:
        t = metric.table if metric and metric.table else {}
        part = str((t.get("top") or {}).get(t.get("part"), ""))
    chosen, missing = pick_step(project, runs, mets, stage, step)
    picked = [{"run_id": r["run_id"], "label": r.get("label"), "source": r.get("source"),
               **{k: chosen[r["run_id"]].get(k) for k in ("stage", "step", "value", "unit", "source_file")}}
              for r in runs if r["run_id"] in chosen]
    found: dict[str, dict] = {}
    tops = {}
    for p in picked:
        rows = db.instances(run_ids=[p["run_id"]], stage=p["stage"], step=p["step"], task=task, name=name)
        held = max((r["depth"] for r in rows), default=depth)
        if depth > held:
            path = project.data / "results" / p["run_id"] / source_path(p["source_file"])
            if not (metric and (metric.area_hier or metric.table) and path.is_file()):
                raise Refuse(f"the database holds {name} down to depth {held}, and {path} cannot be read for depth {depth}")
            rows = parse_instances(metric, path, keep=False)[1]
        mine = [r for r in rows if r["part"] == part]
        if not mine:
            raise Refuse(f"{col[p['run_id']]} has no {name} rows of part '{part}'{_at(p['stage'], p['step'])}; "
                         f"name one with --part: {_few(sorted({r['part'] for r in rows}))}")
        low = min(r["depth"] for r in mine)
        tops[p["run_id"]] = sum(r["value"] for r in mine if r["depth"] == low)
        for r in mine:
            if depth and r["depth"] == depth and (instance is None or fnmatchcase(r["instance"], instance)):
                found.setdefault(r["instance"], {})[p["run_id"]] = r["value"]
    ids = [p["run_id"] for p in picked]
    rows = sorted(({"instance": k, "value": {i: v.get(i) for i in ids}} for k, v in found.items()),
                  key=lambda t: -max(x for x in t["value"].values() if x is not None))
    total = {i: sum(t["value"][i] or 0.0 for t in rows) for i in ids}
    if rows:
        rows += [{"instance": "<sum>", "value": total}, {"instance": "<other>", "value": {i: tops[i] - total[i] for i in ids}}]
    if rows or ids and not depth:
        rows.append({"instance": "<top>", "value": tops})
    unit = picked[0]["unit"] if picked else ""
    for t in rows:
        base = t["value"][ids[0]]
        t["delta"] = {i: None if base is None and t["value"][i] is None else (t["value"][i] or 0.0) - (base or 0.0)
                      for i in ids[1:]}
        t["unit"] = unit
    return {"metric": name, "task": task, "part": part, "depth": depth, "unit": unit, "runs": picked, "rows": rows,
            "missing": missing}


def step_names(project: Project) -> dict[int, str]:
    """Step number to step name over every stage with steps."""
    return {n: project.stages[stage].steps[n] for stage, rng in owned_steps(project).items() for n in rng
            if n < len(project.stages[stage].steps)}


def last_areas(project: Project | None, db: Database, run_ids: list[str], max_depth: int = 3) -> dict[str, Row]:
    """The instance rows of a stage metric of each run for compare.html, at its step of record, else at its last
    step: {run id: {stage, step, source_file, rows}}. A metric of the project comes before a name it no longer has.
    A run that lacks its step of record has none.

    A row is [instance, depth, value], down to `max_depth`, so the page stays small.
    """
    out = {}
    for rid in run_ids:
        tops = [t for t in db.instances(run_ids=[rid], task="", depth=0) if t["part"] == ""]
        name = min({t["name"] for t in tops}, key=lambda n: (project is None or n not in project.metrics, n), default=None)
        top = pick_step(project, [{"run_id": rid}], [t for t in tops if t["name"] == name])[0].get(rid)
        if top is None:
            continue
        rows = [[a["instance"], a["depth"], a["value"]]
                for a in db.instances(run_ids=[rid], stage=top["stage"], step=top["step"], task="", name=name)
                if a["part"] == "" and a["depth"] <= max_depth]
        out[rid] = {"stage": top["stage"], "step": top["step"], "source_file": top.get("source_file"), "rows": rows}
    return out


def run_heads(picked: list[Row]) -> list[str]:
    """The column name of each run: its name, with its stage and step."""
    col = names(picked)
    return [col[p["run_id"]] + _at(p["stage"], p["step"]) for p in picked]


def _change(base: float | None, v: float | None) -> str | None:
    """The percent of `v` to `base`: `new` without a base, `gone` without `v`."""
    return None if base is None and v is None else "new" if base is None else "gone" if v is None else _pct(v, base)


def instances_view(view: Row) -> RenderableType:
    """Instance, one column per run with its stage and step, and the delta and the percent of each run to the first;
    the source file of each run and the runs that lack the step below."""
    picked, missing = view["runs"], view["missing"]
    if not view["rows"]:
        return _with_missing("no instance rows", missing)
    col, ids = names(picked), [p["run_id"] for p in picked]
    head = ["instance", *run_heads(picked)]
    for i in ids[1:]:
        head += [f"Δ {col[i]}", "Δ %"]
    body = []
    for t in view["rows"]:
        v = t["value"]
        line = [t["instance"], *[board.num(v[i]) for i in ids]]
        for i in ids[1:]:
            line += [board.num(t["delta"][i]), _change(v[ids[0]], v[i])]
        body.append(line)
    title = view["metric"] + (f" in {view['unit']}" if view["unit"] else "") + f" at depth {view['depth']}"
    title += "".join(f", {k} {view[k]}" for k in ("task", "part") if view[k])
    src = Text("\n".join(f"{col[p['run_id']]}: {p['source_file']}" for p in picked), style="dim")
    return _with_missing(Group(Text(title, style="bold"), board.table(head, body, right=tuple(head[1:])), src), missing)


# metrics per step

def step_name(project: Project | None, stage: str, step: int | None) -> str | None:
    """The name of a step number from the stage's `steps` list."""
    steps = project.stages[stage].steps if project and stage in project.stages else []
    return steps[step] if step is not None and 0 <= step < len(steps) else None


def verdict(project: Project | None, m: Row) -> str | None:
    """`FAIL` or `pass` for a metric row under the `pass` rule of its metric; None without a rule or a value."""
    metric = project.metrics.get(m["name"]) if project else None
    return metric.verdict(m.get("value")) if metric else None


def mark(value: float | None, verdict: str | None, source: str | None = None) -> str | None:
    """A value as `board.num` prints it, with FAIL next to a value that breaks its pass rule; `failed: <source>` for a
    row without a value, whose source is the error."""
    if value is None:
        return None if source is None else f"failed: {source}"
    return board.num(value) + (" FAIL" if verdict == "FAIL" else "")


def side_by_side(project: Project | None, runs: list[Row], rows: list[Row], stage: str | None = None,
                 step: int | None = None) -> tuple[list[Row], list[Row]]:
    """One row per task and metric, with the value of each run at the step that `pick_step` takes and the delta of each
    to the first; and the runs that lack the step."""
    ids = [r["run_id"] for r in runs]
    groups: dict[tuple, list[Row]] = {}
    for m in rows:
        groups.setdefault((m.get("task") or "", m["name"]), []).append(m)
    out, missing = [], []
    for (task, name), ms in sorted(groups.items()):
        chosen, lack = pick_step(project, runs, ms, stage, step)
        missing += lack
        if not chosen:
            continue
        row: Row = {"task": task, "metric": name, "unit": ms[0].get("unit"), **{k: {} for k in (
            "value", "stage", "step", "step_name", "source_file", "verdict")}}
        for rid, m in chosen.items():
            for k in ("value", "stage", "step", "source_file"):
                row[k][rid] = m.get(k)
            row["step_name"][rid] = step_name(project, m["stage"], m.get("step"))
            row["verdict"][rid] = verdict(project, m)
        base = row["value"].get(ids[0])
        row["delta"] = {i: None if row["value"].get(i) is None or base is None else round(row["value"][i] - base, 9)
                        for i in ids[1:]}
        out.append(row)
    return out, missing


def cell(row: Row, run_id: str) -> str | None:
    """The value of one run in a side-by-side row, with FAIL and its stage and step."""
    v = row["value"].get(run_id)
    return None if v is None else f"{mark(v, row['verdict'][run_id])}{_at(row['stage'][run_id], row['step'][run_id])}"


def side_by_side_view(runs: list[Row], rows: list[Row], missing: list[Row]) -> RenderableType:
    """metric, task, the value of each run with its stage and step, and the percent of each run to the first; the runs
    that lack the step below."""
    if not rows:
        return _with_missing("no metrics", missing)
    col = names(runs)
    ids = [r["run_id"] for r in runs]
    head = ["metric", "task", *[col[i] for i in ids]]
    for i in ids[1:]:
        head += [f"Δ {col[i]}", "Δ %"]
    body = []
    for r in rows:
        line = [r["metric"], r["task"], *[cell(r, i) for i in ids]]
        for i in ids[1:]:
            line += [board.num(r["delta"][i]), _pct(r["value"].get(i), r["value"].get(ids[0]))]
        body.append(line)
    return _with_missing(board.table(head, body, styles={"metric": "bold"}, right=tuple(head[2:])), missing)


def over_steps(project: Project | None, rows: list[Row]) -> list[Row]:
    """One row per step of one run: stage, step, step name, and each metric with a step."""
    out: dict[tuple, Row] = {}
    for m in rows:
        if m.get("step") is None or m.get("task"):
            continue
        row = out.setdefault((m["step"], m["stage"]), {"stage": m["stage"], "step": m["step"],
                                                       "step_name": step_name(project, m["stage"], m["step"]),
                                                       "value": {}, "source_file": {}, "verdict": {}})
        row["value"][m["name"]] = m["value"]
        row["source_file"][m["name"]] = m.get("source_file")
        row["verdict"][m["name"]] = verdict(project, m)
    return [out[k] for k in sorted(out)]


def over_steps_view(rows: list[Row]) -> RenderableType:
    """A column per metric, and a verdict when a metric has a pass rule; with one metric, its change from the
    step before and its source file."""
    if not rows:
        return "no metric with a step"
    keys = sorted({k for r in rows for k in r["value"]})
    head = ["stage", "step", "name", *keys]
    body = [[r["stage"], r["step"], r["step_name"],
             *[mark(r["value"].get(k), r["verdict"].get(k), r["source_file"].get(k)) for k in keys]] for r in rows]
    if any(v for r in rows for v in r["verdict"].values()):
        head.append("verdict")
        for line, r in zip(body, rows):
            got = {v for v in r["verdict"].values() if v}
            line.append("FAIL" if "FAIL" in got else "pass" if got else None)
    if len(keys) == 1:
        head += ["Δ", "source"]
        prev = None
        for line, r in zip(body, rows):
            v = r["value"].get(keys[0])
            line += [None if v is None or prev is None else board.num(v - prev), r["source_file"].get(keys[0])]
            prev = v if v is not None else prev
    return board.table(head, body, styles={"source": "dim"}, right=("step", *keys, "Δ"))


# runtime

def dur(seconds: float | None) -> str | None:
    """A duration as 45s, 12m or 7h32m."""
    if seconds is None:
        return None
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def _log_steps(project: Project, run: Row, stage: str, first: int) -> list[Row]:
    """Step starts from the stage's step_log in the collected files; the source is file:line.

    With `{step}` in the file, each file is the log of one step, and its first match starts that step. In one log of
    several steps, the matches count on from `first`, unless group 2 names the step. The last match of a file gets
    `end`, the mtime of the file."""
    spec = project.stages[stage].step_log
    values = {k: v for k, v in run.items() if isinstance(v, (str, int, float))}
    try:
        rel = config.render(spec["file"], {**values, "step": "{step}"})
    except ConfigError:
        return []
    run_dir = project.data / "results" / str(run["run_id"])
    per_step = "{step}" in rel
    rx, out, n = re.compile(spec["regex"]), [], first
    for step, path in find_files(rel, run_dir, "*" if per_step else None):
        found = []
        with path.open(errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if m := rx.search(line):
                    k = int(m.group(2)) if rx.groups >= 2 and m.group(2) is not None else step if per_step else n
                    n = k + 1
                    found.append({"stage": stage, "step": k, "started": int(float(m.group(1))),
                                  "source": f"{path.relative_to(run_dir)}:{i}"})
                    if per_step:
                        break
        if found:
            found[-1]["end"] = int(path.stat().st_mtime)
        out += found
    return out


def _wall(started: float | None, end: float | None) -> float | None:
    return end - started if started and end and end >= started else None


def runtime(project: Project, db: Database, run: Row, now: float | None = None) -> Row:
    """Stage, step and task times of one run, from stage_runs, step_runs and the step_log files.

    A step starts at its line in the stage's step_log, else when the driver first saw its number; a step outside the
    steps of its stage is left out. It ends when the next step of its stage starts, else when its stage ends, else at
    the mtime of the file its line is the last start of. A stage without an end counts up to now while the run lives,
    else up to its last heartbeat, and so does its last step. A time that counts up to now, or that has no end, is
    open; `open` names each, and the total is then a lower bound."""
    run_id = run["run_id"]
    live = board.is_live(run) and run.get("state") != "dead"
    until = (time.time() if now is None else now) if live else run.get("updated")
    rows = db.stage_runs(run_id)
    # The driver records `[runtime] setup` as the stage `setup`, before every stage of the flow.
    order = {"setup": -1, **{n: i for i, n in enumerate(project.stages)}}
    stages = sorted((r for r in rows if not r["task"]), key=lambda r: (order.get(r["stage"], len(order)), r["attempt"]))
    last: dict[str, Row] = {}
    for r, nxt in zip(stages, [*stages[1:], None]):
        # An attempt without an end ended when the next attempt started.
        end = r.get("ended") or (nxt["started"] if nxt and nxt["stage"] == r["stage"] else None)
        r.update(wall_s=_wall(r.get("started"), end or until), open=not end and (live or until is None),
                 source="stage_runs")
        last[r["stage"]] = r
    owned = owned_steps(project)
    found = {(r["stage"], r["step"]): {**r, "source": "step_runs"} for r in db.step_runs(run_id)}
    for name, st in project.stages.items():
        if st.step_log:
            own = owned.get(name)
            # The flow's own line beats the poll: a progress command can see a step early.
            found.update(((name, s["step"]), s) for s in _log_steps(project, run, name, own.start if own else 0))
    steps = sorted((s for (stage, k), s in found.items() if stage not in owned or k in owned[stage]),
                   key=lambda s: (order.get(s["stage"], len(order)), s["step"]))
    for s, nxt in zip(steps, [*steps[1:], None]):
        st = last.get(s["stage"])
        if nxt and nxt["stage"] == s["stage"]:
            end, s["open"] = nxt["started"], False
        elif st is not None:
            end, s["open"] = st.get("ended") or until, st["open"]
        else:
            end = s.get("end")
            s["open"] = end is None
        s.pop("end", None)
        s["name"] = step_name(project, s["stage"], s["step"])
        s["wall_s"] = _wall(s["started"], end)
    tasks: dict[str, Row] = {}
    for r in rows:
        if r["task"] and r.get("started") and r.get("ended"):
            t = tasks.setdefault(r["stage"], {"stage": r["stage"], "tasks": 0, "wall_s": 0, "longest": None,
                                             "longest_s": 0, "source": "stage_runs"})
            w = r["ended"] - r["started"]
            t["tasks"] += 1
            t["wall_s"] += w
            if w > t["longest_s"]:
                t["longest"], t["longest_s"] = r["task"], w
    total = sum(s["wall_s"] or 0 for s in stages) if stages else (
        sum(s["wall_s"] or 0 for s in steps) if steps else None)
    opened = [" ".join(str(x) for x in (s["stage"], s["step"], s["name"]) if x is not None) for s in steps if s["open"]]
    opened += [r["stage"] for r in stages if r["open"] and r["stage"] not in {s["stage"] for s in steps if s["open"]}]
    return {"run_id": run_id, "label": run.get("label"), "source": run.get("source"), "host": run.get("host"),
            "stages": stages, "steps": steps, "tasks": list(tasks.values()), "total_s": total, "open": opened}


def _what(text: str | None, row: Row) -> str | None:
    return ", ".join(t for t in (text, "open" if row["open"] else None) if t) or None


def runtime_view(rt: Row) -> RenderableType:
    """One run: a row per stage attempt, its steps under it, its tasks summed, and the total; an open time says so,
    and the total then reads as a lower bound."""
    body = []
    steps = rt["steps"]
    names = [s["stage"] for s in rt["stages"]] + [s["stage"] for s in steps if s["stage"] not in
                                                   {x["stage"] for x in rt["stages"]}]
    for name in dict.fromkeys(names):
        for s in (x for x in rt["stages"] if x["stage"] == name):
            body.append([name, None, _what(f"attempt {s['attempt']}, {s.get('status') or '-'}", s),
                         board._ts(s.get("started")), dur(s["wall_s"]), s["source"]])
        for s in (x for x in steps if x["stage"] == name):
            body.append([name, s["step"], _what(s["name"], s), board._ts(s["started"]), dur(s["wall_s"]), s["source"]])
        for t in (x for x in rt["tasks"] if x["stage"] == name):
            body.append([name, None, f"{t['tasks']} tasks, longest {t['longest']} {dur(t['longest_s'])}", None,
                         dur(t["wall_s"]), t["source"]])
    if not body:
        return "no stage or step times"
    body.append(["total", None, f"at least, open: {', '.join(rt['open'])}" if rt["open"] else None, None,
                 dur(rt["total_s"]), None])
    return Group(Text(f"{rt['label']}  {rt['run_id']}", style="bold"),
                 board.table(["stage", "step", "what", "started", "wall", "source"], body,
                             styles={"started": "dim", "source": "dim"}, right=("step", "wall")))


def runtime_batch_view(project: Project, rts: list[Row]) -> RenderableType:
    """One row per run: the wall time of each stage, attempts summed, and the total; an `open` column names the open
    times of a run whose total is a lower bound."""
    names = [n for n in project.stages if any(s["stage"] == n for rt in rts for s in rt["stages"] or rt["steps"])]
    opened = any(rt["open"] for rt in rts)
    body = []
    for rt in rts:
        per = {n: sum(s["wall_s"] or 0 for s in (rt["stages"] or rt["steps"]) if s["stage"] == n) or None for n in names}
        body.append([rt["label"], rt.get("source"), rt.get("host"), *[dur(per[n]) for n in names], dur(rt["total_s"]),
                     *([", ".join(rt["open"]) or None] if opened else [])])
    if not body:
        return "no runs"
    return board.table(["label", "source", "host", *names, "total", *(["open"] if opened else [])], body,
                       styles={"label": "bold", "source": "dim"}, right=(*names, "total"))


# host and run samples

def host_history(samples: list[Row], batch: str = "", runs: dict[str, list[list[Row]]] | None = None) -> list[Row]:
    """One row per host: the sample count, the time span, and the cores, RAM and scratch in use over it.

    `runs` maps a host to the run samples of each run of `batch` there, and the row of such a host gets `batch`: the
    CPU cores, the RSS and the tree size those runs used together at each host sample. A run counts its last sample
    at or before that time, from its first sample up to the first host sample after its last one."""
    out: dict[str, Row] = {}
    for s in samples:
        h = out.setdefault(s["host"], {"host": s["host"], "samples": [], "cores": s["cores"], "ram_gb": s["ram_gb"],
                                       "scratch_gb": s["scratch_gb"], "gpus": s["gpus"]})
        h["samples"].append(s)
    for h in out.values():
        ss = h["samples"]
        h.update(first=ss[0]["ts"], last=ss[-1]["ts"], source="host_samples",
                 cores_used=[[s["ts"], min(s["load"] or 0, s["cores"] or 0)] for s in ss],
                 ram_used_gb=[[s["ts"], s["ram_used_gb"]] for s in ss],
                 scratch_used_gb=[[s["ts"], s["scratch_used_gb"]] for s in ss],
                 gpus_busy=[[s["ts"], s["gpus_busy"]] for s in ss])
        if own := [r for r in (runs or {}).get(h["host"], []) if r]:
            h["batch"] = {"batch": batch, "runs": len(own), "source": "run_samples",
                          **_use(own, [s["ts"] for s in ss])}
        del h["samples"]
    return list(out.values())


def _use(runs: list[list[Row]], grid: list[int]) -> Row:
    """The CPU cores, RSS and tree size that `runs`, the samples of each run, used together at each time of `grid`."""
    times = [[s["ts"] for s in ss] for ss in runs]
    out: Row = {"cores_used": [], "ram_used_gb": [], "tree_gb": []}
    # The watcher writes the host sample of a cycle after the run samples, so a live run's last sample precedes it.
    for prev, ts in zip([grid[0] - 1, *grid], grid):
        at = [ss[bisect.bisect_right(t, ts) - 1] for ss, t in zip(runs, times) if t[0] <= ts and t[-1] > prev]
        for key, col, k in (("cores_used", "cpu_pct", 0.01), ("ram_used_gb", "rss_gb", 1), ("tree_gb", "tree_gb", 1)):
            out[key].append([ts, round(sum((s[col] or 0) * k for s in at), 2)])
    return out


def host_history_view(rows: list[Row]) -> RenderableType:
    """Per host: a line of cores in use, RAM in use and scratch in use over the window, with the peak and the last;
    under it, the batch's own line when the host ran runs of the batch."""
    if not rows:
        return "no host samples"

    def cell(points: list, total: float | None) -> str:
        vals = [v for _, v in points if v is not None]
        return f"{board.spark(points, total)} {max(vals):.0f}/{vals[-1]:.0f} of {total or 0:.0f}" if vals else "-"

    body = []
    for h in rows:
        body.append([h["host"], board._ts(h["first"]), board._ts(h["last"]), cell(h["cores_used"], h["cores"]),
                     cell(h["ram_used_gb"], h["ram_gb"]), cell(h["scratch_used_gb"], h["scratch_gb"]),
                     cell(h["gpus_busy"], h["gpus"]) if h.get("gpus") else "-"])
        if b := h.get("batch"):
            body.append([f"  {b['batch']}", "", "", cell(b["cores_used"], h["cores"]), cell(b["ram_used_gb"], h["ram_gb"]),
                         cell(b["tree_gb"], h["scratch_gb"]), ""])
    note = "each line spans the window left to right, from 0 to the host's total"
    if any(h.get("batch") for h in rows):
        note += "; an indented line is the batch's own use: the CPU, the RSS and the tree size of its runs"
    return Group(board.table(["host", "from", "to", "cores (peak/last)", "RAM GB", "scratch GB", "GPUs"], body,
                             styles={"host": "bold", "from": "dim", "to": "dim"}), Text(note, style="dim"))


# parameters

def parameters_differ(db: Database, runs: list[Row]) -> list[Row]:
    """The parameters whose value differs between `runs`, by key: {key, value: {run id: value}}; a run without the
    key has None. The source is left out, since `names` puts it into the name of each run when it differs."""
    have = {r["run_id"]: {p["key"]: p["value"] for p in db.parameters(r["run_id"])} for r in runs}
    keys = sorted({k for v in have.values() for k in v} - {"source"})
    return [{"key": k, "value": {i: v.get(k) for i, v in have.items()}} for k in keys
            if len({v.get(k) for v in have.values()}) > 1]


def parameters_view(runs: list[Row], rows: list[Row]) -> RenderableType:
    """One line per parameter that differs, with the value of each run, and a blank line under it."""
    return Group(board.table(["parameter", *names(runs).values()], [[p["key"], *p["value"].values()] for p in rows],
                             styles={"parameter": "bold"}), Text(""))


# task fields

def field_clashes(rows: list[Row]) -> list[Row]:
    """The task ids that ran with more than one set of fields, from rows of the task_fields table: {task, sets:
    [{fields, runs}]}, where `runs` holds the run ids of one set."""
    by_task: dict[str, dict[str, dict[str, str]]] = {}
    for r in rows:
        by_task.setdefault(r["task"], {}).setdefault(r["run_id"], {})[r["key"]] = r["value"]
    out = []
    for task, runs in sorted(by_task.items()):
        sets: dict[tuple, Row] = {}
        for run_id, fields in runs.items():
            sets.setdefault(tuple(sorted(fields.items())), {"fields": fields, "runs": []})["runs"].append(run_id)
        if len(sets) > 1:
            out.append({"task": task, "sets": list(sets.values())})
    return out


def clash_text(clash: Row, handles: dict[str, str], shown: int = 5) -> str:
    """One line for a task that ran with several sets of fields: each set by the fields that differ, and its runs,
    the first `shown` of them by name."""
    sets = clash["sets"]
    keys = [k for k in sorted({k for s in sets for k in s["fields"]}) if len({s["fields"].get(k) for s in sets}) > 1]

    def runs(ids: list[str]) -> str:
        names = [handles.get(i, i) for i in ids]
        return board.join(names) if len(names) <= shown else f"{', '.join(names[:shown])} and {len(names) - shown} more"

    return f"task {clash['task']} ran with {len(sets)} sets of fields: " + "; ".join(
        " ".join(f'{k}="{s["fields"][k]}"' if k in s["fields"] else f"{k} unset" for k in keys) + f" in {runs(s['runs'])}"
        for s in sets)

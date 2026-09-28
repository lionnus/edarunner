"""Views over the project database: area deltas, metrics per step, runtimes, host and run samples.

Each view returns plain rows for --json and a rich renderable for a person. A row
keeps the source of its number: a file under data/results, or the table it came from.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text

from . import board, config
from .config import ConfigError
from .db import Database
from .metrics import owned_steps
from .model import Project

Row = dict[str, Any]


def names(runs: list[Row]) -> dict[str, str]:
    """A name per run: the label, or label@source when the runs come from more than one source; a prefix of the run
    id follows when two runs share that name."""
    mixed = len({r.get("source") for r in runs}) > 1
    base = {r["run_id"]: f"{r.get('label')}@{r.get('source')}" if mixed else str(r.get("label")) for r in runs}
    twins = Counter(base.values())
    return {i: n if twins[n] == 1 else f"{n} {board.prefix(i, list(base))}" for i, n in base.items()}


def _num(v: float | None, digits: int = 1) -> str | None:
    return None if v is None else f"{v:.{digits}f}"


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


# hierarchical area

def _tops(project: Project | None, area: list[Row]) -> list[Row]:
    """The top rows of one area metric as metric rows; a metric of the project comes before a name it no longer has."""
    name = min({t["name"] for t in area}, key=lambda n: (project is None or n not in project.metrics, n), default=None)
    return [{**t, "value": t["area"]} for t in area if t["name"] == name]


def area_delta(project: Project | None, db: Database, runs: list[Row], depth: int, instance: str | None = None,
               stage: str | None = None, step: int | None = None) -> tuple[list[Row], list[Row], list[Row]]:
    """(the report of each run, one row per instance at `depth` with the area of each run and the delta to the first,
    the runs that lack the step). Each run is at the step that `pick_step` takes for the area metric."""
    tops = _tops(project, db.area(run_ids=[r["run_id"] for r in runs], stage=stage, depth=0))
    chosen, missing = pick_step(project, runs, tops, stage, step)
    picked = [{"run_id": r["run_id"], "label": r.get("label"), "source": r.get("source"),
               **{k: chosen[r["run_id"]].get(k) for k in ("stage", "step", "name", "area", "unit", "source_file")}}
              for r in runs if r["run_id"] in chosen]
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
    return picked, rows, missing


def last_areas(project: Project | None, db: Database, run_ids: list[str], max_depth: int = 3) -> dict[str, Row]:
    """The area report of each run for compare.html, at its step of record, else at its last step:
    {run id: {stage, step, source_file, rows}}. A run that lacks its step of record has none.

    A row is [instance, depth, area], down to `max_depth`, so the page stays small.
    """
    out = {}
    for rid in run_ids:
        top = pick_step(project, [{"run_id": rid}], _tops(project, db.area(run_ids=[rid], depth=0)))[0].get(rid)
        if top is None:
            continue
        rows = [[a["instance"], a["depth"], a["area"]]
                for a in db.area(run_ids=[rid], stage=top["stage"], step=top["step"])
                if a["name"] == top["name"] and a["depth"] <= max_depth]
        out[rid] = {"stage": top["stage"], "step": top["step"], "source_file": top.get("source_file"), "rows": rows}
    return out


def step_names(project: Project) -> dict[int, str]:
    """Step number to step name over every stage with steps."""
    return {n: project.stages[stage].steps[n] for stage, rng in owned_steps(project).items() for n in rng
            if n < len(project.stages[stage].steps)}


def area_view(picked: list[Row], rows: list[Row], depth: int, missing: list[Row]) -> RenderableType:
    """Instance, one area column per run with its stage and step, and the delta of each run to the first; the sources
    and the runs that lack the step below."""
    if not picked or not rows:
        return _with_missing("no area rows", missing)
    col = names(picked)
    ids = [p["run_id"] for p in picked]
    head = ["instance", *[col[p["run_id"]] + _at(p["stage"], p["step"]) for p in picked]]
    for i in ids[1:]:
        head += [f"Δ {col[i]}", "Δ %"]
    total = {p["run_id"]: p["area"] for p in picked}
    body = []
    # At depth 0 the rows are the top itself.
    for t in [*rows, *([{"instance": "<top>", "area": total}] if depth else [])]:
        a = t["area"]
        line = [t["instance"], *[_num(a.get(i)) for i in ids]]
        for i in ids[1:]:
            d = None if a.get(i) is None or a.get(ids[0]) is None else a[i] - a[ids[0]]
            line += [_num(d), _pct(a.get(i), a.get(ids[0]))]
        body.append(line)
    right = tuple(h for h in head if h != "instance")
    src = Text("\n".join(f"{col[p['run_id']]}: {p['source_file']}" for p in picked), style="dim")
    unit = next((p["unit"] for p in picked if p.get("unit")), "")
    return _with_missing(Group(Text(f"area {unit} at depth {depth}".replace("  ", " "), style="bold"),
                               board.table(head, body, right=right), src), missing)


# metrics per step

def step_name(project: Project | None, stage: str, step: int | None) -> str | None:
    """The name of a step number from the stage's `steps` list."""
    steps = project.stages[stage].steps if project and stage in project.stages else []
    return steps[step] if step is not None and 0 <= step < len(steps) else None


def _fmt(v: float | None) -> str | None:
    return None if v is None else f"{v:.6g}"


def verdict(project: Project | None, m: Row) -> str | None:
    """`FAIL` or `pass` for a metric row under the `pass` rule of its metric; None without a rule or a value."""
    metric = project.metrics.get(m["name"]) if project else None
    return metric.verdict(m.get("value")) if metric else None


def mark(cell: object, verdict: str | None) -> object:
    """A value cell, with FAIL next to a value that breaks its pass rule."""
    return f"{cell} FAIL" if verdict == "FAIL" else cell


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
    return None if v is None else f"{mark(_fmt(v), row['verdict'][run_id])}{_at(row['stage'][run_id], row['step'][run_id])}"


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
            line += [_fmt(r["delta"][i]), _pct(r["value"].get(i), r["value"].get(ids[0]))]
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
    body = [[r["stage"], r["step"], r["step_name"], *[mark(_fmt(r["value"].get(k)), r["verdict"].get(k)) for k in keys]]
            for r in rows]
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
            line += [None if v is None or prev is None else _fmt(v - prev), r["source_file"].get(keys[0])]
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
    """Step starts from the stage's step_log in the collected files; the source is file:line."""
    spec = project.stages[stage].step_log
    values = {k: v for k, v in run.items() if isinstance(v, (str, int, float))}
    try:
        rel = config.render(spec["file"], values)
    except ConfigError:
        return []
    path = project.data / "results" / str(run["run_id"]) / rel
    if not path.is_file():
        return []
    rx, out, n = re.compile(spec["regex"]), [], first
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        m = rx.search(line)
        if not m:
            continue
        step = int(m.group(2)) if rx.groups >= 2 and m.group(2) is not None else n
        n = step + 1
        out.append({"stage": stage, "step": step, "started": int(float(m.group(1))), "source": f"{rel}:{i}"})
    # The next line of the log ends a step, even one of a later stage of the same log.
    for s, nxt in zip(out, out[1:]):
        s["end"] = nxt["started"]
    return out


def runtime(project: Project, db: Database, run: Row) -> Row:
    """Stage, step and task times of one run, from stage_runs, step_runs and the step_log files."""
    run_id = run["run_id"]
    rows = db.stage_runs(run_id)
    # The driver records `[runtime] setup` as the stage `setup`, before every stage of the flow.
    order = {"setup": -1, **{n: i for i, n in enumerate(project.stages)}}
    stages = sorted((r for r in rows if not r["task"]), key=lambda r: (order.get(r["stage"], len(order)), r["attempt"]))
    for r in stages:
        r["wall_s"] = r["ended"] - r["started"] if r.get("ended") and r.get("started") else None
        r["source"] = "stage_runs"
    ended = {s["stage"]: s["ended"] for s in stages if s.get("ended")}
    owned = owned_steps(project)
    steps = [{**r, "source": "step_runs"} for r in db.step_runs(run_id)]
    have = {s["stage"] for s in steps}
    for name, st in project.stages.items():
        if st.step_log and name not in have:
            own = owned.get(name)
            logged = {s["step"]: s for s in _log_steps(project, run, name, own.start if own else 0)}
            steps += [s for k, s in sorted(logged.items()) if own is None or k in own]
    steps.sort(key=lambda s: (order.get(s["stage"], len(order)), s["step"]))
    for s, nxt in zip(steps, [*steps[1:], None]):
        end = nxt["started"] if nxt and nxt["stage"] == s["stage"] else ended.get(s["stage"]) or s.pop("end", None)
        s.pop("end", None)
        s["name"] = step_name(project, s["stage"], s["step"])
        s["wall_s"] = end - s["started"] if end and end >= s["started"] else None
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
    return {"run_id": run_id, "label": run.get("label"), "source": run.get("source"), "host": run.get("host"),
            "stages": stages, "steps": steps, "tasks": list(tasks.values()), "total_s": total}


def runtime_view(rt: Row) -> RenderableType:
    """One run: a row per stage attempt, its steps under it, its tasks summed, and the total."""
    body = []
    steps = rt["steps"]
    names = [s["stage"] for s in rt["stages"]] + [s["stage"] for s in steps if s["stage"] not in
                                                   {x["stage"] for x in rt["stages"]}]
    for name in dict.fromkeys(names):
        for s in (x for x in rt["stages"] if x["stage"] == name):
            body.append([name, None, f"attempt {s['attempt']}, {s.get('status') or '-'}", board._ts(s.get("started")),
                         dur(s["wall_s"]), s["source"]])
        for s in (x for x in steps if x["stage"] == name):
            body.append([name, s["step"], s["name"], board._ts(s["started"]), dur(s["wall_s"]), s["source"]])
        for t in (x for x in rt["tasks"] if x["stage"] == name):
            body.append([name, None, f"{t['tasks']} tasks, longest {t['longest']} {dur(t['longest_s'])}", None,
                         dur(t["wall_s"]), t["source"]])
    if not body:
        return "no stage or step times"
    body.append(["total", None, None, None, dur(rt["total_s"]), None])
    return Group(Text(f"{rt['label']}  {rt['run_id']}", style="bold"),
                 board.table(["stage", "step", "what", "started", "wall", "source"], body,
                             styles={"started": "dim", "source": "dim"}, right=("step", "wall")))


def runtime_batch_view(project: Project, rts: list[Row]) -> RenderableType:
    """One row per run: the wall time of each stage, attempts summed, and the total."""
    names = [n for n in project.stages if any(s["stage"] == n for rt in rts for s in rt["stages"] or rt["steps"])]
    body = []
    for rt in rts:
        per = {n: sum(s["wall_s"] or 0 for s in (rt["stages"] or rt["steps"]) if s["stage"] == n) or None for n in names}
        body.append([rt["label"], rt.get("source"), rt.get("host"), *[dur(per[n]) for n in names], dur(rt["total_s"])])
    if not body:
        return "no runs"
    return board.table(["label", "source", "host", *names, "total"], body, styles={"label": "bold", "source": "dim"},
                       right=(*names, "total"))


# host and run samples

def host_history(samples: list[Row]) -> list[Row]:
    """One row per host: the sample count, the time span, and the cores, RAM and scratch in use over it."""
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
        del h["samples"]
    return list(out.values())


def host_history_view(rows: list[Row]) -> RenderableType:
    """Per host: a line of cores in use, RAM in use and scratch in use over the window, with the peak and the last."""
    if not rows:
        return "no host samples"

    def cell(points: list, total: float | None) -> str:
        vals = [v for _, v in points if v is not None]
        return f"{board.spark(points, total)} {max(vals):.0f}/{vals[-1]:.0f} of {total or 0:.0f}" if vals else "-"

    body = [[h["host"], board._ts(h["first"]), board._ts(h["last"]), cell(h["cores_used"], h["cores"]),
             cell(h["ram_used_gb"], h["ram_gb"]), cell(h["scratch_used_gb"], h["scratch_gb"]),
             cell(h["gpus_busy"], h["gpus"]) if h.get("gpus") else "-"] for h in rows]
    return Group(board.table(["host", "from", "to", "cores (peak/last)", "RAM GB", "scratch GB", "GPUs"], body,
                             styles={"host": "bold", "from": "dim", "to": "dim"}),
                 Text("each line spans the window left to right, from 0 to the host's total", style="dim"))


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

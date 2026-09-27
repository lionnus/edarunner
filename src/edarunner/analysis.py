"""Views over the run database: area deltas, metrics per step, runtimes, host and run samples.

Each view returns plain rows for --json and a rich renderable for a person. A row
keeps the source of its number: a file under data/results, or the table it came from.
"""

from __future__ import annotations

import re
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


def last_areas(db: Database, run_ids: list[str], max_depth: int = 3) -> dict[str, Row]:
    """The last area report of each run for compare.html: {run id: {stage, step, source_file, rows}}.

    A row is [instance, depth, area], down to `max_depth`, so the page stays small.
    """
    out = {}
    for rid in run_ids:
        tops = db.area(run_ids=[rid], depth=0)
        if not tops:
            continue
        top = max(tops, key=_step_key)
        rows = [[a["instance"], a["depth"], a["area"]]
                for a in db.area(run_ids=[rid], stage=top["stage"], step=top["step"])
                if a["name"] == top["name"] and a["depth"] <= max_depth]
        out[rid] = {"stage": top["stage"], "step": top["step"], "source_file": top.get("source_file"), "rows": rows}
    return out


def step_names(project: Project) -> dict[int, str]:
    """Step number to step name over every stage with steps."""
    return {n: project.stages[stage].steps[n] for stage, rng in owned_steps(project).items() for n in rng
            if n < len(project.stages[stage].steps)}


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
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM stage_runs WHERE run_id=? ORDER BY started, stage, task, attempt", (run_id,))]
    order = {n: i for i, n in enumerate(project.stages)}
    stages = sorted((r for r in rows if not r["task"]), key=lambda r: (order.get(r["stage"], len(order)), r["attempt"]))
    for r in stages:
        r["wall_s"] = r["ended"] - r["started"] if r.get("ended") and r.get("started") else None
        r["source"] = "stage_runs"
    ended = {s["stage"]: s["ended"] for s in stages if s.get("ended")}
    owned = owned_steps(project)
    steps = [{**dict(r), "source": "step_runs"} for r in db.conn.execute(
        "SELECT stage, step, started FROM step_runs WHERE run_id=? ORDER BY stage, step", (run_id,))]
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
    return {"run_id": run_id, "label": run.get("label"), "src": run.get("src"), "host": run.get("host"),
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
        body.append([rt["label"], rt.get("src"), rt.get("host"), *[dur(per[n]) for n in names], dur(rt["total_s"])])
    if not body:
        return "no runs"
    return board.table(["label", "design", "host", *names, "total"], body, styles={"label": "bold", "design": "dim"},
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

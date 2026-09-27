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

Row = dict[str, Any]


def names(runs: list[Row]) -> dict[str, str]:
    """A column name per run: the label, or label@src when two runs share a label."""
    labels = [r.get("label") for r in runs]
    return {r["run_id"]: str(r.get("label")) if labels.count(r.get("label")) == 1 else f"{r.get('label')}@{r.get('src')}"
            for r in runs}


def _num(v: float | None, digits: int = 1) -> str | None:
    return None if v is None else f"{v:.{digits}f}"


def _pct(v: float | None, base: float | None) -> str | None:
    return None if v is None or not base else f"{(v - base) / abs(base) * 100:+.1f}%"


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

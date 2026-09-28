"""A frozen snapshot of one or more sources for a paper: manifest, three tables, the diff of each dirty source and the
collected files."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, analysis, collect, config
from .board import is_live
from .db import Database, pick
from .guards import Refuse
from .model import Project

Row = dict[str, Any]

RUN_COLUMNS = ["run_id", "label", "config", "build_tag", "source", "host", "phase", "started", "ended", "batch", "dirty",
               "tree_id", "retired"]
PARAMETER_COLUMNS = ["run_id", "label", "source", "key", "value", "origin"]
METRIC_COLUMNS = ["run_id", "label", "config", "source", "stage", "step", "task", "metric", "canonical", "value", "unit", "source_file",
                  "record"]


def export(
    project: Project,
    db: Database,
    sources: list[str],
    out: Path,
    labels: list[str] | None = None,
    dry_run: bool = False,
    with_logs: bool = False,
) -> dict[str, Any]:
    """Write manifest.json, runs.csv, metrics.csv, parameters.csv and the collected files of the runs of `sources` to
    `out`.

    The export holds the `pick` of each label and source. The manifest lists every exported run whose phase is
    not done under `incomplete`, and the other runs of each label and source under `skipped`. The files of a run
    go under its label, or under label@source when the runs come from more than one source. They are copied
    verbatim; `log/` directories and `*.log` files only with `with_logs`. Each dirty source is listed under
    `dirty_sources`, and its `data/sources/<tag>/source.diff` goes to `sources/<tag>/source.diff`.
    """
    if not sources or not all(sources):
        raise Refuse("empty source")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise Refuse(f"'{out}' exists and is not empty")
    runs, skipped = _select(db, sources, labels)
    if not runs:
        raise Refuse(f"no run has the source {' or '.join(map(repr, sources))}" + (f" and a label in {labels}" if labels else ""))
    ids = [r["run_id"] for r in runs]
    metrics = analysis.mark_record(project, db.metrics(run_ids=ids))
    names = analysis.names(runs)
    retired = {b["batch"] for b in db.batches() if b.get("retired")}
    params = [[r["run_id"], r["label"], r["source"], p["key"], p["value"], p["origin"]]
              for r in runs for p in db.parameters(r["run_id"])]
    dirty = [_dirty_source(project, s) for s in sources if "-dirty" in s]

    plan: list[tuple[str, Path | bytes]] = [
        ("runs.csv", to_csv(RUN_COLUMNS, [_run_row(r, retired) for r in runs])),
        ("metrics.csv", to_csv(METRIC_COLUMNS, [metric_row(m) for m in metrics])),
        ("parameters.csv", to_csv(PARAMETER_COLUMNS, params)),
    ]
    plan += [(f"sources/{d['source']}/source.diff", project.data / "sources" / d["source"] / "source.diff")
             for d in dirty if d["diff_sha256"]]
    for r in runs:
        results = Path(project.data) / "results" / r["run_id"]
        for src in sorted(p for p in results.rglob("*") if p.is_file()):
            rel = src.relative_to(results)
            if not with_logs and ("log" in rel.parts[:-1] or rel.suffix == ".log"):
                continue
            plan.append((f"{names[r['run_id']]}/{rel}", src))

    manifest: dict[str, Any] = {
        "producer": f"edarunner {__version__}",
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema": 2,
        "project": project.project,
        "sources": sources,
        "runs": [{**{k: r.get(k) for k in ("run_id", "label", "config", "build_tag", "source", "host", "phase")},
                  "record": _record(project, db, r)} for r in runs],
        "tables": {"runs.csv": len(runs), "metrics.csv": len(metrics), "parameters.csv": len(params)},
        "dirty_sources": dirty,
        "files": [],
        "incomplete": [_ident(r) for r in runs if r.get("phase") != "done"],
        "skipped": [_ident(r) for r in skipped],
    }
    if dry_run:
        for rel, item in plan:
            print(rel)
            manifest["files"].append(_entry(rel, item))
        return manifest

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    tmp.mkdir()
    for rel, item in plan:
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(item, bytes):
            dst.write_bytes(item)
        else:
            shutil.copy2(item, dst)
        manifest["files"].append(_entry(rel, dst))
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    os.replace(tmp, out)
    return manifest


def _record(project: Project, db: Database, r: Row) -> dict[str, Any]:
    """What made a run, as far as edarunner knows it: host, start and end, versions, each stage's times, and the
    command of each stage and task as the spec rendered it."""
    spec = collect.load_spec(project, r)
    rec = spec.get("record") or {}
    stages = [{k: s[k] for k in ("stage", "task", "attempt", "status", "started", "ended")}
              for s in db.stage_runs(r["run_id"]) if not s["task"]]
    commands = [{"stage": st["name"], "task": t.get("id") or "", "cmd": t["cmd"]}
                for st in spec.get("stages") or [] for t in (st, *(st.get("tasks") or [])) if t.get("cmd")]
    return {"host": r.get("host"), "started": r.get("started"), "ended": None if is_live(r) else r.get("updated"),
            "edarunner": rec.get("edarunner"), "driver_sha256": rec.get("driver_sha256"), "tools": rec.get("tools") or {},
            "stages": stages, "commands": commands}


def _dirty_source(project: Project, tag: str) -> Row:
    """A dirty source with its base, the commit of each nested repository and the sha256 of its diff, from
    `data/sources/<tag>/`; the sha256 is None when that directory has no diff."""
    d = project.data / "sources" / tag
    meta, diff = config.load_json(d / "source.json"), d / "source.diff"
    return {"source": tag, "base": meta.get("base") or tag.split("-dirty")[0], "nested": meta.get("nested") or {},
            "diff_sha256": hashlib.sha256(diff.read_bytes()).hexdigest() if diff.is_file() else None}


def _select(db: Database, sources: list[str], labels: list[str] | None) -> tuple[list[Row], list[Row]]:
    """The `pick` of each label and exact source tag in `sources`, in label and source order; and the other runs."""
    groups: dict[tuple[str, str], list[Row]] = {}
    for r in db.runs():
        if r.get("source") in sources and (labels is None or r["label"] in labels):
            groups.setdefault((r["label"], r["source"]), []).append(r)
    picked = [pick(groups[k]) for k in sorted(groups)]
    ids = {r["run_id"] for r in picked}
    return picked, [r for k in sorted(groups) for r in groups[k] if r["run_id"] not in ids]


def _ident(r: Row) -> Row:
    return {k: r.get(k) for k in ("run_id", "label", "source", "phase")}


def _run_row(r: Row, retired: set[str]) -> list[Any]:
    ended = "" if is_live(r) else r.get("updated")
    return [r["run_id"], r.get("label"), r.get("config"), r.get("build_tag"), r.get("source"), r.get("host"), r.get("phase"),
            r.get("started"), ended, r.get("batch"), r.get("dirty"), r.get("tree_id"),
            int(r.get("state") == "retired" or r.get("batch") in retired)]


def metric_row(m: Row) -> list[Any]:
    """One metrics.csv row in the order of METRIC_COLUMNS; `record` is 1 at the step of record, 0 at the other steps of
    a metric with `record`, and empty for a metric without it."""
    return [m["run_id"], m.get("label"), m.get("config"), m.get("source"), m.get("stage"), m.get("step"), m.get("task"),
            m.get("name"), m.get("canonical"), m.get("value"), m.get("unit"), m.get("source_file"), m.get("record")]


def to_csv(head: list[str], rows: list[list[Any]]) -> bytes:
    """CSV with a header line; None becomes an empty field."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    w.writerows([["" if v is None else v for v in row] for row in rows])
    return buf.getvalue().encode()


def _entry(rel: str, item: Path | bytes) -> dict[str, Any]:
    if isinstance(item, bytes):
        return {"path": rel, "bytes": len(item), "sha256": hashlib.sha256(item).hexdigest()}
    with open(item, "rb") as fh:
        return {"path": rel, "bytes": item.stat().st_size, "sha256": hashlib.file_digest(fh, "sha256").hexdigest()}

"""A frozen snapshot of one or more sources for a paper: manifest, six tables, the diff of each dirty source and, on
request, the collected files."""

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
from .metrics import source_path
from .model import Project

Row = dict[str, Any]

RUN_COLUMNS = ["run_id", "label", "config", "build_tag", "source", "host", "phase", "started", "ended", "batch", "dirty",
               "tree_id", "retired", "flags"]
PARAMETER_COLUMNS = ["run_id", "label", "source", "key", "value", "origin"]
TASK_FIELD_COLUMNS = ["run_id", "label", "source", "task", "key", "value", "origin"]
FLAG_COLUMNS = ["run_id", "label", "source", "task", "check", "text"]
METRIC_COLUMNS = ["run_id", "label", "config", "build_tag", "source", "host", "stage", "step", "task", "metric", "canonical", "value",
                  "unit", "source_file", "record"]
INSTANCE_COLUMNS = ["run_id", "label", "source", "stage", "step", "task", "metric", "part", "instance", "depth", "value", "local",
                    "cells", "unit"]


def export(
    project: Project,
    db: Database,
    sources: list[str],
    out: Path,
    labels: list[str] | None = None,
    dry_run: bool = False,
    files: bool = False,
    globs: list[str] | None = None,
) -> dict[str, Any]:
    """Write manifest.json, runs.csv, metrics.csv, parameters.csv, task_fields.csv, instances.csv and flags.csv of the
    runs of `sources` to `out`; with `files` also the collected files that the exported metric rows cite, and the
    collected files that a glob of `globs` matches under `data/results/<run_id>/`.

    The export holds the `pick` of each label and source. The manifest lists every exported run whose phase is
    not done under `incomplete`, and the other runs of each label and source under `skipped`. A collected file is
    copied verbatim to `<run_id>/<path>`. Its entry names the run, and the stage, step and task that the rows citing
    it share, else those of the task directory that holds it; a value they do not share is None. A cited file that
    `data/results` lacks is listed under `missing_files`. Each dirty source is listed under `dirty_sources`, and its
    `data/sources/<tag>/source.diff` goes to `sources/<tag>/source.diff`.
    """
    if not sources or not all(sources):
        raise Refuse("empty source")
    for g in globs or []:
        if not g or Path(g).is_absolute() or ".." in Path(g).parts:
            raise Refuse(f"{g!r} is not a glob under data/results/<run_id>/")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise Refuse(f"'{out}' exists and is not empty")
    runs, skipped = _select(db, sources, labels)
    if not runs:
        raise Refuse(f"no run has the source {' or '.join(map(repr, sources))}" + (f" and a label in {labels}" if labels else ""))
    ids = [r["run_id"] for r in runs]
    metrics = analysis.mark_record(project, db.metrics(run_ids=ids))
    retired = {b["batch"] for b in db.batches() if b.get("retired")}
    params = [[r["run_id"], r["label"], r["source"], p["key"], p["value"], p["origin"]]
              for r in runs for p in db.parameters(r["run_id"])]
    fields = [[r["run_id"], r["label"], r["source"], f["task"], f["key"], f["value"], f["origin"]]
              for r in runs for f in db.task_fields([r["run_id"]])]
    dirty = [_dirty_source(project, s) for s in sources if "-dirty" in s]
    instances = [[i["name" if k == "metric" else k] for k in INSTANCE_COLUMNS] for i in db.instances(run_ids=ids)]
    by_id = {r["run_id"]: r for r in runs}
    flags = [{"run_id": f["run_id"], "label": by_id[f["run_id"]]["label"], "source": by_id[f["run_id"]]["source"],
              **{k: f[k] for k in ("task", "check", "text")}} for f in db.flags(ids)]

    plan: list[tuple[str, Path | bytes, Row]] = [
        ("runs.csv", to_csv(RUN_COLUMNS, [_run_row(r, retired, flags) for r in runs]), {}),
        ("metrics.csv", to_csv(METRIC_COLUMNS, [metric_row(m) for m in metrics]), {}),
        ("parameters.csv", to_csv(PARAMETER_COLUMNS, params), {}),
        ("task_fields.csv", to_csv(TASK_FIELD_COLUMNS, fields), {}),
        ("instances.csv", to_csv(INSTANCE_COLUMNS, instances), {}),
        ("flags.csv", to_csv(FLAG_COLUMNS, [list(f.values()) for f in flags]), {}),
    ]
    plan += [(f"sources/{d['source']}/source.diff", project.data / "sources" / d["source"] / "source.diff", {})
             for d in dirty if d["diff_sha256"]]
    cited: dict[str, dict[str, list[tuple[Any, ...]]]] = {}
    for m in metrics:
        if m["value"] is not None and m["source_file"]:
            cited.setdefault(m["run_id"], {}).setdefault(source_path(m["source_file"]), []).append(
                (m["stage"], m["step"], m["task"]))
    missing: list[Row] = []
    for r in runs:
        rid, results = r["run_id"], project.data / "results" / r["run_id"]
        rows = cited.get(rid, {})
        want = set(rows) if files else set()
        for g in globs or []:
            want.update(p.relative_to(results).as_posix() for p in results.glob(g) if p.is_file())
        tasks = list(collect.spec_tasks(collect.load_spec(project, r), str(r.get("root") or ""))) if want else []
        for rel in sorted(want):
            where = rows.get(rel) or [(s, None, t) for s, t, d in tasks if rel.startswith(d.rstrip("/") + "/")] or [(None, None, "")]
            stage, step, task = (v[0] if len(set(v)) == 1 else None for v in zip(*where))
            entry = {"run_id": rid, "stage": stage, "step": step, "task": task}
            if (results / rel).is_file():
                plan.append((f"{rid}/{rel}", results / rel, entry))
            else:
                missing.append({"path": f"{rid}/{rel}", **entry})

    manifest: dict[str, Any] = {
        "producer": f"edarunner {__version__}",
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema": 4,
        "project": project.project,
        "sources": sources,
        "runs": [{**{k: r.get(k) for k in ("run_id", "label", "config", "build_tag", "source", "host", "phase")},
                  "record": _record(project, db, r)} for r in runs],
        "tables": {"runs.csv": len(runs), "metrics.csv": len(metrics), "parameters.csv": len(params),
                   "task_fields.csv": len(fields), "instances.csv": len(instances), "flags.csv": len(flags)},
        "ge_um2": project.ge_um2 or None,
        "dirty_sources": dirty,
        "flags": flags,
        "files": [],
        "missing_files": missing,
        "incomplete": [_ident(r) for r in runs if r.get("phase") != "done"],
        "skipped": [_ident(r) for r in skipped],
    }
    if dry_run:
        for rel, item, entry in plan:
            print(rel)
            manifest["files"].append({**_entry(rel, item), **entry})
        return manifest

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    tmp.mkdir()
    for rel, item, entry in plan:
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(item, bytes):
            dst.write_bytes(item)
        else:
            shutil.copy2(item, dst)
        manifest["files"].append({**_entry(rel, dst), **entry})
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


def _run_row(r: Row, retired: set[str], flags: list[Row]) -> list[Any]:
    """One runs.csv row; `flags` names the checks that flag the run, separated by spaces."""
    ended = "" if is_live(r) else r.get("updated")
    return [r["run_id"], r.get("label"), r.get("config"), r.get("build_tag"), r.get("source"), r.get("host"), r.get("phase"),
            r.get("started"), ended, r.get("batch"), r.get("dirty"), r.get("tree_id"),
            int(r.get("state") == "retired" or r.get("batch") in retired),
            " ".join(sorted({f["check"] for f in flags if f["run_id"] == r["run_id"]}))]


def metric_row(m: Row) -> list[Any]:
    """One metrics.csv row in the order of METRIC_COLUMNS; `record` is 1 at the step of record, 0 at the other steps of
    a metric with `record`, and empty for a metric without it."""
    return [m.get("name" if k == "metric" else k) for k in METRIC_COLUMNS]


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

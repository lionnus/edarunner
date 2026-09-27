"""A frozen snapshot of one design for a paper: manifest, two tables and the collected files."""

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

from . import __version__, collect
from .board import is_live
from .guards import Refuse
from .db import Database
from .model import Project

Row = dict[str, Any]

RUN_COLUMNS = ["run_id", "label", "config", "build_tag", "design", "host", "phase", "started", "ended"]
METRIC_COLUMNS = ["run_id", "label", "config", "design", "stage", "step", "task", "metric", "canonical", "value", "unit", "source"]


def export(
    project: Project,
    db: Database,
    design: str,
    out: Path,
    labels: list[str] | None = None,
    dry_run: bool = False,
    with_logs: bool = False,
) -> dict[str, Any]:
    """Write manifest.json, runs.csv, metrics.csv and the collected files of `design` to `out`.

    The files are copied verbatim; `log/` directories and `*.log` files only with `with_logs`.
    """
    if not design:
        raise Refuse("empty design")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise Refuse(f"'{out}' exists and is not empty")
    runs = _select(db, design, labels)
    if not runs:
        raise Refuse(f"no run has the source '{design}'" + (f" and a label in {labels}" if labels else ""))
    ids = [r["run_id"] for r in runs]
    metrics = db.metrics(run_ids=ids)

    plan: list[tuple[str, Path | bytes]] = [
        ("runs.csv", _csv(RUN_COLUMNS, [_run_row(r) for r in runs])),
        ("metrics.csv", _csv(METRIC_COLUMNS, [_metric_row(m) for m in metrics])),
    ]
    for r in runs:
        results = Path(project.data) / "results" / r["run_id"]
        for src in sorted(p for p in results.rglob("*") if p.is_file()):
            rel = src.relative_to(results)
            if not with_logs and ("log" in rel.parts[:-1] or rel.suffix == ".log"):
                continue
            plan.append((f"{r['label']}/{rel}", src))

    manifest: dict[str, Any] = {
        "producer": f"edarunner {__version__}",
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema": 1,
        "project": project.project,
        "source": design,
        "runs": [{**{k: r.get(k) for k in ("run_id", "label", "config", "build_tag", "src", "host", "phase")},
                  "record": _record(project, db, r)} for r in runs],
        "tables": {"runs.csv": len(runs), "metrics.csv": len(metrics)},
        "files": [],
        "incomplete": [r["run_id"] for r in runs if is_live(r) or (r.get("counts") or {}).get("failed")],
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
    """What made a run, as far as edarunner knows it: host, start and end, versions, and each stage's times."""
    spec = collect.load_spec(project, r).get("record") or {}
    stages = [{k: s[k] for k in ("stage", "task", "attempt", "status", "started", "ended")} for s in db.conn.execute(
        "SELECT * FROM stage_runs WHERE run_id=? AND task='' ORDER BY started, attempt", (r["run_id"],))]
    return {"host": r.get("host"), "started": r.get("started"), "ended": None if is_live(r) else r.get("updated"),
            "edarunner": spec.get("edarunner"), "driver_sha256": spec.get("driver_sha256"), "tools": spec.get("tools") or {},
            "stages": stages}


def _select(db: Database, design: str, labels: list[str] | None) -> list[Row]:
    """The newest run per label with the exact source tag `design`, in label order."""
    newest: dict[str, Row] = {}
    for r in db.runs():
        if r.get("src") == design and (labels is None or r["label"] in labels):
            newest[r["label"]] = r
    return [newest[k] for k in sorted(newest)]


def _run_row(r: Row) -> list[Any]:
    ended = "" if is_live(r) else r.get("updated")
    return [r["run_id"], r.get("label"), r.get("config"), r.get("build_tag"), r.get("src"), r.get("host"), r.get("phase"),
            r.get("started"), ended]


def _metric_row(m: Row) -> list[Any]:
    return [m["run_id"], m.get("label"), m.get("config"), m.get("src"), m.get("stage"), m.get("step"), m.get("task"),
            m.get("name"), m.get("canonical"), m.get("value"), m.get("unit"), m.get("source_file")]


def _csv(head: list[str], rows: list[list[Any]]) -> bytes:
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

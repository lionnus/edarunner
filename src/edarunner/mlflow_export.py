"""The project database as a local MLflow tracking store, for the MLflow UI.

One MLflow run per edarunner run, in one experiment per project. Needs the
`mlflow` extra; the package imports nothing from here otherwise.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import analysis
from .board import is_live
from .db import Database
from .guards import Refuse
from .model import Project

Row = dict[str, Any]
MAX_ARTIFACT_BYTES = 1 << 20
_KEY = re.compile(r"[^\w\-. /]")
_KILLED = ("STOPPED", "KILLED", "ABANDONED")


def _key(text: str) -> str:
    return _KEY.sub("_", text)


def export_mlflow(project: Project, db: Database, out: Path, source: str | None = None,
                  max_bytes: int = MAX_ARTIFACT_BYTES) -> dict[str, Any]:
    """Write every run (or the runs of `source`) into the store under `out`; a run already there is skipped."""
    try:
        from mlflow.entities import Metric, Param, RunTag
        from mlflow.tracking import MlflowClient
    except ImportError:
        raise Refuse("the MLflow export needs mlflow: pip install 'edarunner[mlflow]'") from None
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # MLflow's SQL store needs SQLite 3.31; an older one gets the file store in mlruns/.
    new_sqlite = tuple(int(x) for x in sqlite3.sqlite_version.split(".")) >= (3, 31)
    uri = f"sqlite:///{out / 'mlflow.db'}" if new_sqlite else (out / "mlruns").as_uri()
    if not new_sqlite:
        os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    client = MlflowClient(tracking_uri=uri)
    exp = client.get_experiment_by_name(project.project)
    exp_id = exp.experiment_id if exp else client.create_experiment(project.project,
                                                                     artifact_location=(out / "artifacts").as_uri())
    done = {r.data.tags.get("edr.run_id") for r in client.search_runs([exp_id], max_results=50000)}
    runs = [r for r in db.runs() if source is None or r.get("source") == source]
    written, skipped = [], []
    for r in runs:
        if r["run_id"] in done:
            skipped.append(r["run_id"])
            continue
        started = int(r.get("started") or 0) * 1000
        tags = {"edr.run_id": r["run_id"], "edr.project": project.project, "edr.source": r.get("source") or "",
                "edr.batch": r.get("batch") or "", "edr.label": r.get("label") or "", "edr.host": r.get("host") or "",
                "edr.phase": r.get("phase") or "", "mlflow.runName": r.get("label") or r["run_id"]}
        run = client.create_run(exp_id, start_time=started or None, tags=tags)
        rid = run.info.run_id
        params = {p["key"]: p["value"] for p in db.parameters(r["run_id"])}
        params.update({k: r.get(k) for k in ("config", "build_tag", "source") if r.get(k) and k not in params})
        metrics = []
        for m in db.metrics(run_ids=[r["run_id"]]):
            if m["value"] is None:
                continue
            key = _key(m["name"] + (f"/{m['task']}" if m.get("task") else ""))
            metrics.append(Metric(key, float(m["value"]), int(m.get("extracted_at") or 0) * 1000,
                                  int(m["step"]) if m.get("step") is not None else 0))
        rt = analysis.runtime(project, db, r)
        ts = started or 0
        for s in rt["stages"]:
            if s["wall_s"] is not None:
                metrics.append(Metric(_key(f"runtime_s/{s['stage']}"), float(s["wall_s"]), ts, int(s["attempt"])))
        for s in rt["steps"]:
            if s["wall_s"] is not None:
                metrics.append(Metric("runtime_s/step", float(s["wall_s"]), ts, int(s["step"])))
        if rt["total_s"] is not None:
            metrics.append(Metric("runtime_s/total", float(rt["total_s"]), ts, 0))
        for i in range(0, len(metrics), 900):  # log_batch takes 1000 entries at most
            client.log_batch(rid, metrics=metrics[i:i + 900],
                             params=[Param(_key(k), str(v)[:6000]) for k, v in params.items()] if i == 0 else [],
                             tags=[RunTag("edr.metrics", str(len(metrics)))] if i == 0 else [])
        if not metrics:
            client.log_batch(rid, params=[Param(_key(k), str(v)[:6000]) for k, v in params.items()])
        results = project.data / "results" / r["run_id"]
        files = sorted(p for p in results.rglob("*") if p.is_file() and p.stat().st_size <= max_bytes) \
            if results.is_dir() else []
        for f in files:
            rel = f.relative_to(results).parent
            client.log_artifact(rid, str(f), None if str(rel) == "." else str(rel))
        if not is_live(r):
            phase = str(r.get("phase") or "")
            status = "FINISHED" if phase == "done" else "KILLED" if phase.startswith(_KILLED) else "FAILED"
            client.set_terminated(rid, status=status, end_time=int(r.get("updated") or 0) * 1000 or None)
        written.append({"run_id": r["run_id"], "mlflow_run": rid, "metrics": len(metrics), "artifacts": len(files)})
    return {"tracking_uri": uri, "experiment": project.project, "written": written, "skipped": skipped}

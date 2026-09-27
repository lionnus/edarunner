"""The briefing of `edr brief`: the project, or one run, as Markdown for a person or an agent new to it.

`project_data` and `run_data` gather plain dicts from the database, the config and the state
directory; `project_text` and `run_text` turn them into sentences. `--json` prints the dicts.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from . import analysis, board, watch
from .model import SCHEDULERS, Project

Row = dict[str, Any]
DOCS_URL = "https://lionnus.github.io/edarunner/"
EVENTS = 10
LOG_LINES = 20

_BACKEND = {"ssh": "edr starts each driver on a site host over ssh",
            "local": "edr starts every driver on this machine"}


def ago(seconds: float | None) -> str:
    """A duration in words: 40 seconds, 12 minutes, 3 hours, 2 days."""
    if seconds is None:
        return "an unknown time"
    s = int(max(0, seconds))
    n, unit = (s, "second") if s < 120 else (s // 60, "minute") if s < 7200 else (s // 3600, "hour") if s < 172800 \
        else (s // 86400, "day")
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _when(ts: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "an unknown time"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _code(items: list[str]) -> str:
    return _join([f"`{i}`" for i in items])


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{'one' if n == 1 else n} {one if n == 1 else many or one + 's'}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# gathering

def _sources(project: Project, batches: list[Row]) -> list[Row]:
    """The checked-out trees under the worktrees directory, each with the batches that use it."""
    wt = project.source.worktrees
    tags = sorted(p.name for p in wt.iterdir() if p.is_dir()) if wt.is_dir() else []
    return [{"tag": t, "path": str(wt / t), "batches": [b["batch"] for b in batches if b.get("source") == t]}
            for t in tags]


def _stages(project: Project) -> list[Row]:
    return [{"stage": name, "group": st.is_group, "parallel": st.parallel if st.is_group else None,
             "steps": len(st.steps), "collect": list(st.collect), "on_request": sorted(st.collect_on_request),
             "tools": sorted(st.needs.tools)} for name, st in project.stages.items()]


def _hosts(project: Project, conn: Any) -> list[Row]:
    """The site hosts with the marks of their last sample in host_samples; no sample means not probed."""
    last = {r["host"]: dict(r) for r in conn.execute(
        "SELECT * FROM host_samples WHERE (host, ts) IN (SELECT host, max(ts) FROM host_samples GROUP BY host)")}
    marks, out = project.site.marks, []
    for name in project.site.hosts:
        s = last.get(name)
        if s is None:
            out.append({"host": name, "probed": None})
            continue

        def frac(used: float | None, total: float | None) -> float:
            return (used or 0) / total if total else 0.0

        m = {"cores": board.resource_mark(frac(s["load"], s["cores"]), marks.cores),
             "ram": board.resource_mark(frac(s["ram_used_gb"], s["ram_gb"]), marks.ram),
             "scratch": board.resource_mark(frac(s["scratch_used_gb"], s["scratch_gb"]), marks.scratch)}
        if s.get("gpus"):
            m["gpu"] = board.resource_mark(frac(s["gpus_busy"], s["gpus"]), marks.gpu)
        out.append({"host": name, "probed": s["ts"], "marks": m, "sample": s})
    return out


def _step(m: Row) -> int:
    return -1 if m.get("step") is None else int(m["step"])


def _state_row(r: Row, hb: dict, now: float) -> Row:
    state = r["state"] if r.get("state") in ("retired", "abandoned") else board.state_of(r)
    return {"handle": board.handle(r), "run_id": r["run_id"], "batch": r["batch"], "state": state,
            "phase": r.get("phase"), "stage": r.get("stage"), "step": r.get("step"), "step_name": hb.get("step_name"),
            "host": r.get("host"), "age_s": None if r.get("updated") is None else int(now - r["updated"]),
            "command": board.triage_cmd(r, state, hb if state == "dead" else {})}


def project_data(c: Any, tools: list[Row]) -> Row:
    """Everything the project briefing says, as one dict; `c` is the command context of cli.py."""
    project, now = c.project, time.time()
    rows = board.order(c.rows())
    batches = c.db.batches()
    runs = [_state_row(r, c.heartbeat(r) if board.is_live(r) else {}, now) for r in rows]
    per_batch: dict[str, Counter] = {}
    for r in runs:
        per_batch.setdefault(r["batch"], Counter())[r["state"]] += 1
    names = {r["run_id"]: board.handle(r) for r in c.db.runs()}
    events = [{**e, "run": names.get(e["run_id"], e["run_id"] or "")} for e in c.db.events(n=EVENTS)]
    read = [str(project.root / f) for f in ("CLAUDE.md", "AGENTS.md") if (project.root / f).is_file()]
    return {
        "project": project.project, "root": str(project.root), "repo": str(project.source.repo),
        "worktrees": str(project.source.worktrees), "sources": _sources(project, batches),
        "backend": project.site.scheduler.backend, "stages": _stages(project),
        "hosts": _hosts(project, c.db.conn), "tools": tools,
        "batches": [{"batch": b, "source": next((x.get("source") for x in batches if x["batch"] == b), None),
                     "states": dict(n)} for b, n in per_batch.items()],
        "live": [r for r in runs if board.is_live({"phase": r["phase"]}) and r["state"] != "queued"],
        "decisions": [r for r in runs if r["command"]],
        "events": events, "read": read, "docs": DOCS_URL,
    }


def run_data(c: Any, row: Row, file_host: str) -> Row:
    """Everything the story of one run says, as one dict; `row` carries the state the board gives it."""
    now, hb = time.time(), c.heartbeat(row)
    state = _state_row(row, hb, now)
    names = {r["run_id"]: board.handle(r) for r in c.db.runs()}
    events = [{**e, "run": names.get(e["run_id"], e["run_id"])} for e in c.db.events(run_id=row["run_id"], n=1000)]
    last: dict[tuple, Row] = {}
    for m in c.db.metrics(run_ids=[row["run_id"]]):
        key = (m["stage"], m.get("task") or "", m.get("canonical") or m["name"])
        if key not in last or _step(m) >= _step(last[key]):
            last[key] = m
    log, tail, note = hb.get("log"), str(hb.get("last_log") or ""), ""
    if log:
        rc, out, err = c.ssh.run(file_host, ["tail", "-n", str(LOG_LINES), str(log)])
        tail, note = (out, "") if rc == 0 else ("", f"tail {log} on {file_host} failed: {err.strip() or f'rc {rc}'}")
    st = watch.STATES.get(state["state"])
    return {
        "handle": state["handle"], "run_id": row["run_id"], "label": row.get("label"), "batch": row["batch"],
        "config": row.get("config"), "src": row.get("src"), "host": row.get("host"), "root": row.get("root"),
        "state": state["state"], "phase": row.get("phase"), "stage": row.get("stage"), "step": row.get("step"),
        "step_name": hb.get("step_name"), "age_s": state["age_s"], "started": row.get("started"),
        "exit": row.get("exit"), "runtime": analysis.runtime(c.project, c.db, row), "events": events,
        "log": log, "log_tail": tail, "log_note": note, "metrics": list(last.values()),
        "command": state["command"], "reason": st.test if st else "",
    }


# text

def _stage_line(s: Row) -> str:
    kind = f"is a task group that runs {_count(s['parallel'], 'task')} at a time" if s["group"] else \
        "runs one command" + (f" through {_count(s['steps'], 'step')}" if s["steps"] else "")
    parts = [kind]
    parts.append(f"collects {_code(s['collect'])}" if s["collect"] else "collects nothing when it ends")
    if s["on_request"]:
        parts.append(f"keeps {_code(s['on_request'])} for `edr continue --collect`")
    if s["tools"]:
        parts.append(f"needs {'the tool' if len(s['tools']) == 1 else 'the tools'} {_code(s['tools'])}")
    return f"- `{s['stage']}` {_join(parts)}."


def _host_line(h: Row, now: float) -> str:
    if h["probed"] is None:
        return f"- `{h['host']}` has not been probed yet."
    marks = ", ".join(f"{k} {v}" for k, v in h["marks"].items())
    return f"- `{h['host']}` at the probe {ago(now - h['probed'])} ago: {marks}."


def _tool_line(t: Row) -> str:
    hosts = ", ".join(f"`{h}`" + (f" ({v})" if v else "") for h, v in t["hosts"].items()) or "no host"
    if "free" in t:
        seats = f"has {t['free']} of {t['total']} seats free" if t.get("total") is not None else f"has {t['free']} seats free"
    elif "note" in t:
        seats = f"could not be probed ({t['note']})"
    elif t.get("total") is not None:
        seats = f"has {t['total']} seats and no probe"
    else:
        seats = "has no seat count"
    return f"- `{t['tool']}` {seats}; it is on {hosts}."


def _states(counts: dict[str, int]) -> str:
    return _join([f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: board.RANK.get(kv[0], 7))])


def _event_line(e: Row) -> str:
    run = f" on `{e['run']}`" if e.get("run") else ""
    text = str(e.get("text") or "").replace("\n", " ")
    return f"- {_when(e['ts'])}, {e['actor']} recorded `{e['kind']}`{run}" + (f": {text}" if text else ".")


def _where(r: Row) -> str:
    where = f"stage `{r['stage']}`" if r.get("stage") else f"phase `{r.get('phase') or 'setup'}`"
    if r.get("step") is not None:
        where += f" at step {r['step']}" + (f" ({r['step_name']})" if r.get("step_name") else "")
    return where


def project_text(d: Row, now: float | None = None) -> str:
    """The project briefing as Markdown."""
    now = now or time.time()
    out = [f"# {d['project']}", ""]
    intro = f"This is the edarunner project `{d['project']}` in `{d['root']}`. Its flow comes from the git " \
            f"repository `{d['repo']}`."
    if not d["sources"]:
        intro += f" No source is checked out under `{d['worktrees']}` yet; `edr checkout` adds one."
    backend = d["backend"]
    how = _BACKEND.get(backend) or (f"edr hands each run to the {backend} scheduler" if backend in SCHEDULERS
                                    else f"the backend is `{backend}`")
    out += [intro + f" The backend is `{backend}`: {how}.", ""]
    if d["sources"]:
        out += [f"{_cap(_count(len(d['sources']), 'source is', 'sources are'))} checked out under `{d['worktrees']}`:",
                "", *(f"- `{s['tag']}`, " + (f"used by {'batch' if len(s['batches']) == 1 else 'batches'} "
                                            f"{_code(s['batches'])}" if s["batches"] else "used by no batch")
                      for s in d["sources"]), ""]
    if d["stages"]:
        out += ["## The flow", "", "A run passes these stages in this order:", "",
                *(_stage_line(s) for s in d["stages"]), ""]
    if d["hosts"] or d["tools"]:
        out += ["## The site", ""]
        if d["hosts"]:
            out += [("The hosts, with the marks of their last probe: 🟢 has room, 🟡 is filling up, 🟠 is nearly "
                    "full and 🔴 is full. The thresholds come from `[marks]`."), "",
                    *(_host_line(h, now) for h in d["hosts"]), ""]
        if d["tools"]:
            out += ["The tools, with the seats their probe reports now:", "", *(_tool_line(t) for t in d["tools"]), ""]
    out += ["## The state", ""]
    if not d["batches"]:
        out += ["There are no runs yet. `edr plan <batch>` and `edr launch <batch>` start the first ones.", ""]
    else:
        for b in d["batches"]:
            n = sum(b["states"].values())
            src = f" on source `{b['source']}`" if b.get("source") else ""
            out.append(f"Batch `{b['batch']}`{src} has {n} {'run' if n == 1 else 'runs'}: {_states(b['states'])}.")
        out.append("")
        if d["live"]:
            out += [f"{_cap(_count(len(d['live']), 'run has', 'runs have'))} not finished:", ""]
            out += [f"- `{r['handle']}` is {r['state']} in {_where(r)} on `{r['host'] or 'no host yet'}`; its last "
                    f"heartbeat is {ago(r['age_s'])} old." for r in d["live"]]
            out.append("")
        else:
            out += ["Every run has finished.", ""]
        if d["decisions"]:
            by_cmd: dict[str, list[Row]] = {}
            for r in d["decisions"]:
                by_cmd.setdefault(r["command"], []).append(r)
            out += ["These runs need a decision; the triage proposes one command for each:", ""]
            for cmd, rs in by_cmd.items():
                who = _join([f"`{r['handle']}` ({r['state']})" for r in rs])
                out.append(f"- {who}: `{cmd}`")
            out += ["", "`docs/reference/states.md` says what to check before you run a proposed command.", ""]
    if d["events"]:
        out += ["The last event:" if len(d["events"]) == 1 else f"The last {len(d['events'])} events, oldest first:", "",
                *(_event_line(e) for e in d["events"]), ""]
    out += ["## Read more", ""]
    local = [f"`{p}`" for p in d["read"]]
    out.append((f"This project keeps its own notes in {_join(local)}. " if local else "")
               + f"The edarunner documentation is at {d['docs']}, and `edr brief --run <handle>` tells the story "
                 "of one run.")
    return "\n".join(out).rstrip() + "\n"


def run_text(d: Row) -> str:
    """The story of one run as Markdown."""
    out = [f"# {d['handle']}", ""]
    ident = f"`{d['handle']}` is the run `{d['run_id']}` of batch `{d['batch']}`. It builds the configuration " \
            f"`{d['config'] or '-'}` from source `{d['src'] or '-'}`"
    ident += f" on host `{d['host']}`, in `{d['root']}`." if d.get("root") else f" on host `{d['host'] or '-'}`."
    if board.is_live({"phase": d["phase"]}):
        ident += f" It is {d['state']} in {_where(d)}, and its last heartbeat is {ago(d['age_s'])} old."
    else:
        ident += f" It ended with the phase `{d['phase']}`" + (f" and exit {d['exit']}" if d.get("exit") is not None
                                                                 else "") + f", so its state is {d['state']}."
    out += [ident, ""]
    rt = d["runtime"]
    if rt["stages"] or rt["steps"]:
        out += ["## Phases", ""]
        seen = [s["stage"] for s in rt["stages"]] + [s["stage"] for s in rt["steps"]]
        for name in dict.fromkeys(seen):
            for s in (x for x in rt["stages"] if x["stage"] == name):
                took = f" and ran for {analysis.dur(s['wall_s'])}" if s.get("wall_s") is not None else ", with no end recorded"
                status = f", ending {s['status']}" if s.get("status") and s.get("ended") else ""
                ex = f" with exit {s['exit']}" if s.get("exit") is not None and s.get("ended") else ""
                if name == "setup" and s.get("status") == "skipped":
                    out.append(f"- The runtime setup was skipped at {_when(s.get('started'))}, because the "
                               "`when_changed` files had not changed.")
                    continue
                if name == "setup":
                    out.append(f"- The runtime setup started {_when(s.get('started'))}{took}{status}{ex}.")
                    continue
                out.append(f"- Stage `{name}`, attempt {s['attempt']}, started {_when(s.get('started'))}{took}"
                           f"{status}{ex}.")
            for s in (x for x in rt["steps"] if x["stage"] == name):
                took = f" and took {analysis.dur(s['wall_s'])}" if s.get("wall_s") is not None else ""
                label = f" ({s['name']})" if s.get("name") else ""
                out.append(f"  - Step {s['step']}{label} started {_when(s['started'])}{took}.")
            for t in (x for x in rt["tasks"] if x["stage"] == name):
                out.append(f"  - {_count(t['tasks'], 'task')} ran for {analysis.dur(t['wall_s'])} in total; the "
                           f"longest was `{t['longest']}` at {analysis.dur(t['longest_s'])}.")
        out.append("")
    if d["events"]:
        out += ["## Events", "", *(_event_line(e) for e in d["events"]), ""]
    if d["log_tail"] or d["log_note"]:
        out += ["## Log", ""]
        if d["log_note"]:
            out += [_cap(d["log_note"]) + ".", ""]
        else:
            where = f"The last lines of `{d['log']}`:" if d.get("log") else \
                "The heartbeat names no log file, so these are the last lines it carries:"
            out += [where, "", "```text", d["log_tail"].rstrip(), "```", ""]
    if d["metrics"]:
        out += ["## Metrics", "", "The last value of each metric so far:", ""]
        for m in d["metrics"]:
            where = f"`{m['stage']}`" + (f" step {m['step']}" if m.get("step") is not None else "") + (
                f" task `{m['task']}`" if m.get("task") else "")
            value = f"{m['value']:g}" if isinstance(m["value"], (int, float)) else str(m["value"])
            out.append(f"- `{m.get('canonical') or m['name']}` is {value}{' ' + m['unit'] if m.get('unit') else ''}"
                       f" at {where}.")
        out.append("")
    out += ["## Next", ""]
    if d["command"]:
        out.append(f"The run is {d['state']}, which means {d['reason']}. The triage proposes `{d['command']}`.")
    else:
        out.append(f"The run is {d['state']}" + (f", which means {d['reason']}" if d["reason"] else "")
                   + ". The triage proposes nothing for it.")
    return "\n".join(out).rstrip() + "\n"

"""The briefing of `edr brief`: the project, or one run, as Markdown for a person or an agent new to it.

`project_data` and `run_data` gather plain dicts from the database, the config and the state
directory; `project_text` and `run_text` turn them into sentences. `--json` prints the dicts.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from . import analysis, board, census, runid, watch
from .hosts import HostProbe, floor
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


def _took(row: Row, verb: str) -> str:
    """How long a stage or step of `analysis.runtime` took, as the end of a sentence; an open one says so."""
    if row.get("wall_s") is None:
        return ", with no end recorded"
    return f" and has run for {analysis.dur(row['wall_s'])} so far" if row.get("open") else \
        f" and {verb} {analysis.dur(row['wall_s'])}"


def _code(items: list[str]) -> str:
    return board.join([f"`{i}`" for i in items])


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{'one' if n == 1 else n} {one if n == 1 else many or one + 's'}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# gathering

def _sources(project: Project, runs: list[Row]) -> list[Row]:
    """The checked-out trees under the worktrees directory, each with the batches whose runs use it."""
    wt = project.source.worktrees
    tags = sorted(p.name for p in wt.iterdir() if p.is_dir()) if wt.is_dir() else []
    return [{"tag": t, "path": str(wt / t), "batches": sorted({r["batch"] for r in runs if r.get("source") == t})}
            for t in tags]


def _behind(project: Project, tag: str) -> int | None:
    """The commits of the tracking ref `[source] ref` that the commit of the source `tag` lacks, without its nested and
    dirty parts; None when the tag is not in the checkout form or git cannot count them."""
    t = runid.parse_tag(tag, project.source.nested)
    if not t:
        return None
    try:
        return int(runid.git("rev-list", "--count", f"{t.base}..{project.source.ref}", cwd=project.source.repo))
    except runid.GitError:
        return None


def _stages(project: Project) -> list[Row]:
    return [{"stage": name, "group": st.is_group, "parallel": st.parallel if st.is_group else None,
             "steps": len(st.steps), "collect": list(st.collect), "on_request": sorted(st.collect_on_request),
             "tools": sorted(st.needs.tools)} for name, st in project.stages.items()]


def _hosts(project: Project) -> Row:
    """The site hosts as the last census saw them, with your runs on each; `probed` is None before a census."""
    c = census.read(float("inf"))
    probes = {h: p["error"] if "error" in p else HostProbe(**p) for h, p in (c.get("hosts") or {}).items()
              if h in project.site.hosts}
    if not probes:
        return {"probed": None, "hosts": [], "names": list(project.site.hosts)}
    floors = {h: floor(project.site, h) for h in probes}
    return {"probed": c["ts"], "hosts": census.host_view(probes, c.get("runs") or [], floors, project.placement),
            "names": list(project.site.hosts)}


def _step(m: Row) -> int:
    return -1 if m.get("step") is None else int(m["step"])


def _state_row(r: Row, hb: dict, now: float, everyone: list[Row]) -> Row:
    state = r["state"] if r.get("state") in ("retired", "abandoned") else board.state_of(r)
    return {"handle": board.handle(r, everyone), "run_id": r["run_id"], "batch": r["batch"], "state": state,
            "phase": r.get("phase"), "stage": r.get("stage"), "step": r.get("step"), "step_name": hb.get("step_name"),
            "host": r.get("host"), "age_s": None if r.get("updated") is None else int(now - r["updated"]),
            "command": board.triage_cmd(r, state, hb if state == "dead" else {}, everyone)}


def project_data(c: Any, tools: list[Row]) -> Row:
    """Everything the project briefing says, as one dict; `c` is the command context of cli.py."""
    project, now = c.project, time.time()
    rows = board.order(c.rows())
    everyone = c.db.runs()
    runs = [_state_row(r, c.heartbeat(r) if board.is_live(r) else {}, now, everyone) for r in rows]
    per_batch: dict[str, Counter] = {}
    for r in runs:
        per_batch.setdefault(r["batch"], Counter())[r["state"]] += 1
    tags = {b: sorted({r["source"] for r in rows if r["batch"] == b and r.get("source")}) for b in per_batch}
    lag = {t: _behind(project, t) for t in {t for ts in tags.values() for t in ts}}
    names = board.handles(everyone)
    events = [{**e, "run": names.get(e["run_id"], e["run_id"] or "")} for e in c.db.events(n=EVENTS)]
    read = [str(project.root / f) for f in ("CLAUDE.md", "AGENTS.md") if (project.root / f).is_file()]
    flagged: dict[str, Counter] = {}
    for f in c.db.flags([r["run_id"] for r in rows]):
        flagged.setdefault(f["run_id"], Counter())[f["check"]] += 1
    return {
        "project": project.project, "root": str(project.root), "repo": str(project.source.repo),
        "ref": project.source.ref, "worktrees": str(project.source.worktrees), "sources": _sources(project, everyone),
        "backend": project.site.scheduler.backend, "stages": _stages(project),
        "hosts": _hosts(project), "tools": tools,
        "batches": [{"batch": b, "sources": [{"source": t, "behind": lag[t]} for t in tags[b]], "states": dict(n)}
                    for b, n in per_batch.items()],
        "live": [r for r in runs if board.is_live({"phase": r["phase"]}) and r["state"] != "queued"],
        "decisions": [r for r in runs if r["command"]],
        "flags": [{"handle": names[i], "run_id": i, "checks": dict(n)} for i, n in flagged.items()],
        "events": events, "read": read, "docs": DOCS_URL,
    }


def run_data(c: Any, row: Row, file_host: str) -> Row:
    """Everything the history of one run says, as one dict; `row` carries the state the board gives it."""
    now, hb, everyone = time.time(), c.heartbeat(row), c.db.runs()
    state = _state_row(row, hb, now, everyone)
    names = board.handles(everyone)
    events = [{**e, "run": names.get(e["run_id"], e["run_id"])} for e in c.db.events(run_id=row["run_id"], n=1000)]
    last: dict[tuple, Row] = {}
    for m in c.db.metrics(run_ids=[row["run_id"]]):
        key = (m["stage"], m.get("task") or "", m["name"])
        if key not in last or _step(m) >= _step(last[key]):
            last[key] = m
    log, tail, note = hb.get("log"), str(hb.get("last_log") or ""), ""
    if log:
        rc, out, err = c.ssh.run(file_host, ["tail", "-n", str(LOG_LINES), str(log)])
        tail, note = (out, "") if rc == 0 else ("", f"tail {log} on {file_host} failed: {err.strip() or f'rc {rc}'}")
    st = watch.STATES.get(state["state"])
    return {
        "handle": state["handle"], "run_id": row["run_id"], "label": row.get("label"), "batch": row["batch"],
        "config": row.get("config"), "source": row.get("source"), "host": row.get("host"), "root": row.get("root"),
        "state": state["state"], "phase": row.get("phase"), "stage": row.get("stage"), "step": row.get("step"),
        "step_name": hb.get("step_name"), "age_s": state["age_s"], "started": row.get("started"),
        "exit": row.get("exit"), "runtime": analysis.runtime(c.project, c.db, row), "events": events,
        "log": log, "log_tail": tail, "log_note": note, "metrics": list(last.values()), "flags": c.db.flags([row["run_id"]]),
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
    return f"- `{s['stage']}` {board.join(parts)}."


def _host_line(h: Row) -> str:
    head = f"- {board.START[h['start']]} `{h['host']}`"
    if "error" in h:
        return f"{head} did not answer: {h['error']}"
    out = [f"{head}: {h['free_cores']:g} of {h['cores']} cores, {h['free_ram_gb']:.0f} of {h['total_ram_gb']:.0f} GB RAM "
           f"and {h['free_gb']:.0f} of {h['total_gb']:.0f} GB scratch are free"]
    if h["why"]:
        out.append(f"No run can start there: {h['why']}")
    if h["runs"]:
        out.append("Your runs there: " + ", ".join(f"{p} {n}" for p, n in sorted(h["runs"].items())) +
                   f", using {h['our_cores']:g} cores, {h['our_ram_gb']:g} GB RAM and {h['our_gb']:g} GB scratch")
    if h["note"]:
        out.append(_cap(h["note"]))
    return ". ".join(out) + "."


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
    return board.join([f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: board.RANK.get(kv[0], 7))])


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
    if d["hosts"]["names"] or d["tools"]:
        out += ["## The site", ""]
        if d["hosts"]["names"] and not d["hosts"]["hosts"]:
            out += ["No census has probed the hosts yet; `edr hosts` probes them now.", ""]
        if d["hosts"]["hosts"]:
            out += [(f"The hosts at the census {ago(now - d['hosts']['probed'])} ago, the ones where a run of this project "
                     "can start first: 🟢 a run can start there, 🔴 none can, and ⚫ the host did not answer."), "",
                    *(_host_line(h) for h in d["hosts"]["hosts"]), ""]
        if d["tools"]:
            out += ["The tools, with the seats their probe reports now:", "", *(_tool_line(t) for t in d["tools"]), ""]
    out += ["## The state", ""]
    if not d["batches"]:
        out += ["There are no runs yet. `edr plan <batch>` and `edr launch <batch>` start the first ones.", ""]
    else:
        for b in d["batches"]:
            n = sum(b["states"].values())
            tags = [f"`{s['source']}` (" + ("lag unknown" if s["behind"] is None else
                                           f"{_count(s['behind'], 'commit')} behind `{d['ref']}`") + ")"
                    for s in b["sources"]]
            src = f" Its {'source is' if len(tags) == 1 else 'sources are'} {board.join(tags)}." if tags else ""
            out.append(f"Batch `{b['batch']}` has {n} {'run' if n == 1 else 'runs'}: {_states(b['states'])}.{src}")
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
                who = board.join([f"`{r['handle']}` ({r['state']})" for r in rs])
                out.append(f"- {who}: `{cmd}`")
            out += ["", "`docs/reference/states.md` says what to check before you run a proposed command.", ""]
        if d["flags"]:
            out += ["The checks flag these runs; `edr brief --run <handle>` lists each flag:", "",
                    *(f"- `{f['handle']}`: " + board.join([f"{n} `{k}`" for k, n in f["checks"].items()]) for f in d["flags"]),
                    ""]
    if d["events"]:
        out += ["The last event:" if len(d["events"]) == 1 else f"The last {len(d['events'])} events, oldest first:", "",
                *(_event_line(e) for e in d["events"]), ""]
    out += ["## Read more", ""]
    local = [f"`{p}`" for p in d["read"]]
    out.append((f"This project keeps its own notes in {board.join(local)}. " if local else "")
               + f"The edarunner documentation is at {d['docs']}, and `edr brief --run <handle>` prints the "
                 "history of one run.")
    return "\n".join(out).rstrip() + "\n"


def run_text(d: Row) -> str:
    """The history of one run as Markdown."""
    out = [f"# {d['handle']}", ""]
    ident = f"`{d['handle']}` is the run `{d['run_id']}` of batch `{d['batch']}`. It builds the configuration " \
            f"`{d['config'] or '-'}` from source `{d['source'] or '-'}`"
    ident += f" on host `{d['host']}`, in `{d['root']}`." if d.get("root") else f" on host `{d['host'] or '-'}`."
    if board.is_live({"phase": d["phase"]}):
        ident += f" It is {d['state']} in {_where(d)}, and its last heartbeat is {ago(d['age_s'])} old."
    else:
        ident += f" It ended with the phase `{d['phase']}`" + (f" and exit {d['exit']}" if d.get("exit") is not None
                                                                 else "") + f", so its state is {d['state']}."
    out += [ident, ""]
    if d["flags"]:
        out += ["## Flags", "", "The checks flag this run:", "",
                *(f"- `{f['check']}`" + (f" on task `{f['task']}`" if f["task"] else "") + f": {f['text']}"
                  for f in d["flags"]), ""]
    rt = d["runtime"]
    if rt["stages"] or rt["steps"]:
        out += ["## Phases", ""]
        seen = [s["stage"] for s in rt["stages"]] + [s["stage"] for s in rt["steps"]]
        for name in dict.fromkeys(seen):
            for s in (x for x in rt["stages"] if x["stage"] == name):
                took = _took(s, "ran for")
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
                label = f" ({s['name']})" if s.get("name") else ""
                out.append(f"  - Step {s['step']}{label} started {_when(s['started'])}{_took(s, 'took')}.")
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
            if m["value"] is None:
                out.append(f"- `{m['name']}` failed at {where}: {m.get('source_file')}")
                continue
            out.append(f"- `{m['name']}` is {board.num(m['value'])}{' ' + m['unit'] if m.get('unit') else ''}"
                       f" at {where}.")
        out.append("")
    out += ["## Next", ""]
    if d["command"]:
        out.append(f"The run is {d['state']}, which means {d['reason']}. The triage proposes `{d['command']}`.")
    else:
        out.append(f"The run is {d['state']}" + (f", which means {d['reason']}" if d["reason"] else "")
                   + ". The triage proposes nothing for it.")
    return "\n".join(out).rstrip() + "\n"

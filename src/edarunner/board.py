"""Boards: 48-column text, the wide table, one run's detail, status.html and compare.html.

A row is a `runs` row of the database; `counts` may be a dict or JSON text.
The tables are rich renderables; `plain` turns one into text for a bot.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import shlex
import sys
import time
from collections import Counter
from decimal import Decimal
from string import Template
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from .db import pick

Row = dict[str, Any]

# compare.html loads Plotly from this file next to it when a user put a copy there, else from the CDN.
PLOTLY_FILE = "plotly.min.js"
PLOTLY_URL = "https://cdn.plot.ly/plotly-2.35.2.min.js"
TERMINAL = ("done", "INCOMPLETE", "FAILED", "OVER_BUDGET", "STOPPED", "KILLED", "ABANDONED")
# Sort rank on a board; the live rows go before the finished ones.
RANK = {"dead": 0, "failed": 0, "hung": 1, "incomplete": 1, "looping": 2, "over_budget": 3,
         "host_full": 4, "killed": 4, "superseded": 5, "stopped": 5, "stale": 6, "running": 8, "done": 9}
_SHORT = {"running": "RUN", "dead": "DEAD", "hung": "HUNG", "looping": "LOOP", "over_budget": "OVER",
          "host_full": "FULL", "superseded": "SUPER", "incomplete": "INC", "failed": "FAIL", "stopped": "STOP",
          "killed": "KILL"}
STYLE = {"running": "green", "queued": "cyan", "stale": "yellow", "host_full": "yellow", "superseded": "yellow",
         "dead": "red", "hung": "red", "looping": "red", "over_budget": "red", "orphan": "red", "failed": "red",
         "incomplete": "magenta", "done": "dim", "retired": "dim", "stopped": "dim", "killed": "dim",
         "imported": "dim", "abandoned": "dim", "resumed": "cyan", "pending": "cyan", "held": "magenta",
         "suspended": "yellow"}
# The mark of a host: a run can start there, none can, or the host did not answer.
START = {True: "🟢", False: "🔴", None: "⚫"}
# No markup: a task in a metric key looks like a tag, `power_w[k_small]`.
_OPTS = {"markup": False, "highlight": False, "emoji": False}


# rendering

def console() -> Console:
    """Stdout: colour on a terminal, none in a pipe or under NO_COLOR, and no wrap in a pipe."""
    tty = sys.stdout.isatty()
    return Console(color_system=None if os.environ.get("NO_COLOR") else "auto", width=None if tty else 400, **_OPTS)


def plain(renderable: str | RenderableType, width: int = 200) -> str:
    """A renderable as text without colour, for a bot message or a test."""
    if isinstance(renderable, str):
        return renderable
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=False, color_system=None, **_OPTS).print(renderable)
    return "\n".join(ln.rstrip() for ln in buf.getvalue().splitlines())


def state_text(state: Any) -> Text:
    """A state or a stage status in its colour."""
    return Text(_s(state) or "-", style=STYLE.get(_s(state), ""))


def bar(used: float, total: float, width: int = 8) -> Text:
    """A used-of-total bar: green below 70 %, yellow below 90 %, red above."""
    frac = min(1.0, max(0.0, used / total)) if total else 0.0
    n = round(frac * width)
    return Text("\u2588" * n + "\u2591" * (width - n), style="green" if frac < 0.7 else "yellow" if frac < 0.9 else "red")


SPARK = "▁▂▃▄▅▆▇█"


def spark(points: list[tuple[int, float | None]], top: float | None = None, width: int = 24) -> str:
    """(time, value) pairs as `width` block characters over the time span, 0 to `top`; a bin without a value is a space."""
    vals = [v for _, v in points if v is not None]
    if not vals:
        return ""
    t0, t1 = points[0][0], points[-1][0]
    bins: list[list[float]] = [[] for _ in range(width)]
    for ts, v in points:
        if v is not None:
            bins[min(width - 1, int((ts - t0) * width / (t1 - t0 or 1)))].append(v)
    top = top or max(vals) or 1
    return "".join(" " if not b else SPARK[min(7, max(0, round(sum(b) / len(b) / top * 7)))] for b in bins)


# row helpers

def is_live(row: Row) -> bool:
    """True while the phase is not terminal."""
    return not str(row.get("phase") or "").startswith(TERMINAL)


def state_of(row: Row) -> str:
    """The watcher's `state` for a live run; the phase class, lower case, for a finished one."""
    if is_live(row):
        return str(row.get("state") or "running")
    return str(row["phase"]).split(":", 1)[0].lower()


def order(rows: list[Row]) -> list[Row]:
    """Board order: live before finished, dead first, then by label. `#n` counts this order."""
    return sorted(rows, key=lambda r: (not is_live(r), RANK.get(state_of(r), 7), _s(r.get("label")), _s(r.get("run_id"))))


def cost(row: Row, now: float | None = None) -> float:
    """The core hours the run has reserved: its elapsed hours times `row['cores']`.

    `cores` is what the run asked for when it started: the most cores that any of its stages needs,
    where a task group counts its `parallel` tasks. A scheduler holds that many for the whole job, so a
    stage that needs fewer still counts them all. A row without cores counts one, the default of
    `needs.cores`."""
    started = row.get("started")
    if not started:
        return 0.0
    end = (now or time.time()) if is_live(row) else (row.get("updated") or started)
    return max(0.0, end - started) / 3600 * float(row.get("cores") or 1)


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _counts(row: Row) -> dict[str, int]:
    c = row.get("counts") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _fd(row: Row) -> str:
    c = _counts(row)
    return f"{c.get('failed', 0)}f/{c.get('done', 0)}d"


def _age_s(row: Row, now: float) -> float | None:
    return None if row.get("updated") is None else max(0.0, now - row["updated"])


def hm(seconds: float | None) -> str:
    """A duration as 3m, 5h or 2d; '-' for None."""
    if seconds is None:
        return "-"
    s = int(seconds)
    return f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def num(value: float | None) -> str | None:
    """A number as every view prints it: six significant digits, every digit before the point, and no exponent, so
    123.4560000000001 prints as 123.456 and 48213000000.0 as 48213000000. None stays None."""
    if value is None:
        return None
    text = f"{value + 0.0:.6g}"  # + 0.0 prints -0.0 as 0
    if "e" in text:
        text = f"{value:.0f}" if abs(value) >= 1 else f"{Decimal(text):f}"
    return text


def _ts(t: float | None) -> str:
    return "-" if not t else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _stage_step(row: Row, totals: dict[str, int] | None = None) -> str:
    """`stage/step`; `stage, starting` for a live stage with steps before its first step."""
    stage = _s(row.get("stage"))
    if stage and row.get("step") is None and is_live(row) and stage in (totals or {}):
        return f"{stage}, starting"
    return "-" if not stage else stage if row.get("step") is None else f"{stage}/{row['step']}"


def table(head: list[str], body: list[list[Any]], styles: dict[str, str] | None = None,
          right: tuple[str, ...] = ()) -> Table:
    """Columns under a rule; `styles` per column name, `right` names the right-aligned ones. None prints as '-'."""
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, collapse_padding=True)
    for h in head:
        t.add_column(h, style=(styles or {}).get(h), justify="right" if h in right else "left")
    for r in body:
        t.add_row(*(c if isinstance(c, Text) else "-" if c is None else str(c) for c in r))
    return t


# text boards

def narrow(rows: list[Row], width: int = 48, now: float | None = None, totals: dict[str, int] | None = None) -> str:
    """Two lines per live run, dead first, in `width` columns. For a phone."""
    now = now or time.time()
    ordered = order(rows)
    live = [r for r in ordered if is_live(r)]
    counts = Counter(state_of(r) for r in ordered)
    head = time.strftime("%d.%m %H:%M", time.localtime(now)) + " " + " ".join(
        f"{_SHORT.get(k, k)}:{v}" for k, v in sorted(counts.items(), key=lambda kv: RANK.get(kv[0], 7)))
    lines = [head[:width], "-" * width]
    for n, r in enumerate(live, 1):
        st = state_of(r)
        lines.append(f"{'#' + str(n):>3} {_SHORT.get(st, st)[:5]:<5} {_s(r.get('label'))[:14]:<14} "
                     f"{_s(r.get('host'))[:8]:<8} {_s(r.get('phase'))[:14]}"[:width])
        lines.append(f"    {_s(r.get('batch'))[-12:]:<12} {hm(_age_s(r, now)):>4} ago {_stage_step(r, totals)[:10]:<10} "
                     f"{_fd(r)}"[:width])
    if not live:
        lines.append("nothing live")
    return "\n".join(lines)


def narrow_text(rows: list[Row], width: int = 48, now: float | None = None, totals: dict[str, int] | None = None) -> Text:
    """The narrow board with the state of each run in its colour."""
    text = Text(narrow(rows, width, now, totals))
    for m in re.finditer(r"(?m)^ *#\d+ (\S+)", text.plain):
        state = next((s for s in STYLE if _SHORT.get(s, s)[:5] == m.group(1)), None)
        if state:
            text.stylize(STYLE[state], m.start(1), m.end(1))
    return text


def handle(row: Row, runs: list[Row] | None = None) -> str:
    """label@batch. With `runs`, the runs of the project, it is the shortest unique prefix of the run id when
    another of them has the same label and batch, since `db.resolve` refuses such a label@batch."""
    h = f"{_s(row.get('label'))}@{_s(row.get('batch'))}"
    others = [r for r in runs or [] if r["run_id"] != row.get("run_id")]
    if not any(handle(r) == h for r in others):
        return h
    return prefix(row["run_id"], [r["run_id"] for r in others])


def handles(runs: list[Row]) -> dict[str, str]:
    """{run id: handle} of every run of `runs`, the runs of the project, as `handle` gives it."""
    twins = Counter(handle(r) for r in runs)
    return {r["run_id"]: handle(r, runs) if twins[handle(r)] > 1 else handle(r) for r in runs}


def prefix(run_id: str, ids: list[str]) -> str:
    """The shortest prefix of `run_id` that no other id of `ids` starts with; all of it when it starts another id."""
    return run_id[:1 + max((len(os.path.commonprefix([run_id, i])) for i in ids if i != run_id), default=0)]


def join(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def cols(head: list[str], body: list[list[Any]], width: int = 40) -> str:
    """Plain columns in `width`: the first left-aligned and cut to fit, the rest right-aligned. None prints as '-'."""
    rows = [head] + [["-" if c is None else _s(c) for c in r] for r in body]
    w = [max(len(r[i]) for r in rows) for i in range(len(head))]
    gap = " " * (2 if sum(w) + 2 * (len(w) - 1) <= width else 1)
    w[0] = max(1, min(w[0], width - sum(w[1:]) - len(gap) * (len(w) - 1)))
    return "\n".join(gap.join([r[0][:w[0]].ljust(w[0]), *(c.rjust(x) for c, x in zip(r[1:], w[1:]))])[:width].rstrip()
                     for r in rows)


STOP_FLAGS = {"hung": "--why hung", "looping": "--why looping", "over_budget": "--why over-budget",
              "host_full": "--now --why host-full", "superseded": "--after-task --why superseded", "held": "--why held"}


def triage_cmd(row: Row, state: str, hb: dict, runs: list[Row] | None = None) -> str | None:
    """The one command a person runs next for a run in `state`; None for a running run, an orphan, and a run
    without a tree whose next command would be a retire, which would only mark its row. With `runs`, the runs of
    the project, the handle in the command names this run alone."""
    h = handle(row, runs)
    if state in ("running", "orphan", "retired", "abandoned"):
        return None
    if state == "queued":
        return f"edr launch {row['batch']} --only {row['label']}"
    if state in ("stale", "pending", "suspended"):
        return f"edr status {h} --live"
    if state == "dead":
        return f"edr continue {h} --stage {hb.get('stage') or row.get('stage')}" + (
            f" --from {hb['step_name']}" if hb.get("step_name") else "")
    if state in STOP_FLAGS:
        return f"edr stop {h} {STOP_FLAGS[state]}"
    if state == "done":
        return f"edr export --source {row.get('source')} --out exports/{row.get('source')}"
    if not row.get("root"):
        return None
    # The reason names what was observed, the phase the run ended with; without one the person writes it.
    return f"edr retire {h} --why {shlex.quote(str(row.get('phase') or '<why>'))}"


def wide(rows: list[Row], now: float | None = None, totals: dict[str, int] | None = None,
         extra: dict[str, dict[str, Any]] | None = None) -> Table | str:
    """One line per run, every state, in board order; `totals` holds the stages with steps, and `extra` one more
    column per key with the cell of each run by run id.

    Rows of several projects carry `project`, which then takes the place of the row number."""
    now, extra = now or time.time(), extra or {}
    first = "project" if any("project" in r for r in rows) else "#"
    body = [[r.get("project") if first == "project" else f"#{n}", r.get("label"), r.get("source"), r.get("host"),
             state_text(state_of(r)), _s(r.get("phase"))[:40] or None, _stage_step(r, totals), hm(_age_s(r, now)), _fd(r),
             f"{cost(r, now):.1f}", *[col.get(r["run_id"]) for col in extra.values()]] for n, r in enumerate(order(rows), 1)]
    if not body:
        return "no runs"
    return table([first, "label", "source", "host", "state", "phase", "stage/step", "age", "fail/done", "core-h", *extra],
                 body, styles={"label": "bold", "source": "dim", "age": "dim"}, right=("age", "fail/done", "core-h", *extra))


def samples_table(samples: list[Row]) -> Table | None:
    """CPU, RSS, tree size and free disk of a run over its life: a line each, with the peak and the last value."""
    body = []
    for key, name, unit in (("cpu_pct", "CPU", "%"), ("rss_gb", "RSS", "GB"), ("tree_gb", "tree", "GB"),
                            ("disk_free_gb", "disk free", "GB")):
        pts = [(s["ts"], s[key]) for s in samples if s.get(key) is not None]
        if pts:
            vals = [v for _, v in pts]
            body.append([name, spark(pts), f"{max(vals):.4g}", f"{vals[-1]:.4g}", unit, len(pts)])
    return table(["sample", "over the run", "peak", "last", "unit", "n"], body, right=("peak", "last", "n")) if body else None


def run_detail(row: Row, stage_rows: list[Row], metrics: list[Row], tail: str, now: float | None = None,
               gate: str | None = None, samples: list[Row] | None = None, flags: list[Row] | None = None) -> Group:
    """One run: identity, state, what its tool gate waits for, counts, the flags of its checks, stage rows, samples,
    metrics and the log tail."""
    now = now or time.time()
    ex = row.get("exit")
    parts: list[RenderableType] = [
        Text(_s(row.get("run_id")) or "?", style="bold"),
        Text.assemble(state_text(state_of(row)), f"  {_s(row.get('phase')) or '-'}  {_stage_step(row)}" if ex is None
                      else f"  {_stage_step(row)}  driver exit {ex} ({_s(row.get('phase')) or '-'})"),
        Text(f"label {_s(row.get('label'))}  config {_s(row.get('config'))}  batch {_s(row.get('batch'))}  "
             f"source {_s(row.get('source'))}{' dirty' if row.get('dirty') else ''}"),
        Text.assemble(f"host {_s(row.get('host'))}  root ", (_s(row.get("root")), "dim")),
        Text.assemble("started ", (_ts(row.get("started")), "dim"), "  updated ", (_ts(row.get("updated")), "dim"),
                      f" ({hm(_age_s(row, now))} ago)  cost {cost(row, now):.1f} core-h"),
        Text(f"disk free {_s(row.get('disk_free_gb')) or '-'} GB  tree {_s(row.get('tree_gb')) or '-'} GB"),
    ]
    if gate and is_live(row):
        parts.insert(2, Text(f"gate waits for {gate}", style="yellow"))
    if any(s.get("task") for s in stage_rows):
        parts.insert(-1, Text("tasks " + (" ".join(f"{k} {v}" for k, v in _counts(row).items()) or "-")))
    if row.get("killed_by"):
        parts.append(Text(f"killed by {row['killed_by']}", style="red"))
    if flags:
        parts += [Text(""), Text("flags", style="bold red"),
                  table(["check", "task", "text"], [[f["check"], f["task"], f["text"]] for f in flags])]
    if stage_rows:
        parts += [Text(""), Text("stages", style="bold"), table(
            ["stage", "task", "attempt", "status", "command exit", "started", "ended", "took", "signature"],
            [[s.get("stage"), s.get("task"), s.get("attempt"), state_text(s.get("status")), s.get("exit"),
              _ts(s.get("started")), _ts(s.get("ended")),
              hm(s["ended"] - s["started"]) if s.get("ended") and s.get("started") else None, s.get("signature")]
             for s in stage_rows],
            styles={"started": "dim", "ended": "dim"}, right=("took",))]
    sample_tab = samples_table(samples or [])
    if sample_tab is not None:
        first, last = _ts(samples[0]["ts"]), _ts(samples[-1]["ts"])
        parts += [Text(""), Text(f"samples {first} to {last}, from run_samples", style="bold"), sample_tab]
    if metrics:
        parts += [Text(""), Text("metrics", style="bold"), table(
            ["stage", "step", "task", "name", "value", "unit"],
            [[m.get("stage"), m.get("step"), m.get("task"), m.get("name"),
              f"failed: {m.get('source_file')}" if m.get("value") is None else num(m["value"]), m.get("unit")]
             for m in metrics], right=("step", "value"))]
    if tail:
        parts += [Text(""), Text("log tail", style="bold"), Text(tail.rstrip())]
    return Group(*parts)


# html pages

def plotly_src(folder: str | os.PathLike) -> str:
    """The Plotly script of a page in `folder`: PLOTLY_FILE when that file is there, else PLOTLY_URL."""
    return PLOTLY_FILE if os.path.isfile(os.path.join(folder, PLOTLY_FILE)) else PLOTLY_URL


def _h(value: Any) -> str:
    return html.escape(_s(value))


def _json_block(value: Any) -> str:
    # `</` inside a script block would end it.
    return json.dumps(value, default=str).replace("</", "<\\/")


def _td(cells: list[Any]) -> str:
    return "<tr>" + "".join(f"<td>{_h(c)}</td>" for c in cells) + "</tr>"


def _th(cells: list[Any]) -> str:
    return "<tr>" + "".join(f"<th>{_h(c)}</th>" for c in cells) + "</tr>"


def _svg_lines(series: list[tuple[str, list[tuple[float, float]]]], t0: float, t1: float, w: int = 300,
               h: int = 48) -> str:
    """Polylines of (time, fraction from 0 to 1) points, one per (colour, points), over t0..t1."""
    out = []
    for colour, pts in series:
        xy = " ".join(f"{(ts - t0) / ((t1 - t0) or 1) * w:.1f},{h - min(1.0, max(0.0, v)) * h:.1f}" for ts, v in pts)
        if xy:
            out.append(f'<polyline fill="none" stroke="{colour}" stroke-width="1.5" points="{xy}"/>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img"><rect width="{w}" height="{h}" '
            f'fill="none" stroke="#8884"/>{"".join(out)}</svg>')


def host_charts(samples: list[Row], now: float) -> str:
    """One small chart per host over the last day: cores in use (green) and RAM in use (blue), as a share of the total."""
    by: dict[str, list[Row]] = {}
    for s in samples:
        by.setdefault(str(s["host"]), []).append(s)
    return "".join(
        f"<div class=hc><b>{_h(host)}</b> <small>{_h(len(ss))} samples</small><br>" + _svg_lines([
            ("#2a7", [(s["ts"], min(s["load"] or 0, s["cores"] or 0) / s["cores"]) for s in ss if s["cores"]]),
            ("#38c", [(s["ts"], (s["ram_used_gb"] or 0) / s["ram_gb"]) for s in ss if s["ram_gb"]]),
        ], now - 86400, now) + "</div>" for host, ss in sorted(by.items()))


def status_html(rows: list[Row], events: list[Row], hosts: dict[str, Any], now: float | None = None,
                samples: list[Row] | None = None) -> str:
    """A phone-width page: every run in board order, the last 50 events, the hosts and a chart per host."""
    now = now or time.time()
    label_of = {r.get("run_id"): r.get("label") for r in rows}
    run_rows = "".join(
        f"<tr class=\"s-{_h(state_of(r))}\"><td class=st>{_h(state_of(r))}</td>"
        f"<td>{_h(r.get('label'))}<br><small>{_h(r.get('run_id'))}</small></td><td>{_h(r.get('host'))}</td>"
        f"<td>{_h(r.get('phase'))}<br><small>{_h(_stage_step(r))} {_h(_fd(r))}</small></td>"
        f"<td>{_h(hm(_age_s(r, now)))}</td><td>{_h(r.get('batch'))}</td></tr>"
        for r in order(rows))
    latest = sorted(events, key=lambda e: (e.get("id") or 0, e.get("ts") or 0), reverse=True)[:50]
    event_rows = "".join(_td([_ts(e.get("ts")), e.get("actor"), label_of.get(e.get("run_id"), e.get("run_id")),
                              e.get("kind"), e.get("text")]) for e in latest)
    keys: list[str] = []
    for v in hosts.values():
        if isinstance(v, dict):
            keys += [k for k in v if k not in keys]
    keys = keys or ["value"]
    host_rows = _th(["host", *keys]) + "".join(
        _td([name, *([v.get(k, "") for k in keys] if isinstance(v, dict) else [v, *[""] * (len(keys) - 1)])])
        for name, v in sorted(hosts.items()))
    charts = host_charts(samples or [], now)
    if charts:
        charts = "<h3>hosts, last day</h3><p><small>green: cores in use, blue: RAM in use, of the host's total</small></p>" + charts
    return _STATUS.substitute(written=_h(_ts(now)), n=len(rows), runs=run_rows, events=event_rows, hosts=host_rows,
                              charts=charts)


def compare_html(runs: list[Row], parameters: list[Row], metrics: list[Row], plotly_src: str | None,
                 areas: dict[str, Row] | None = None, step_names: dict[int, str] | None = None,
                 tick: list[str] | None = None, better: dict[str, str] | None = None) -> str:
    """A self-contained page over the lists; the plots need `plotly_src`, the tables do not.

    The page opens with the runs of `tick` ticked, the first as the base of every percent, and the runs table folded.
    Without `tick`, it ticks two done runs of the newest source, one per label and the older first, and the runs table
    is open. A URL hash `#runs=<run id or label@source>,...` ticks those runs instead, where label@source is the run
    that `db.pick` takes. A run with `retired` set carries a mark.

    The compare table shows each run at the metric row whose `record` is 1, from `analysis.mark_record`, and `missing`
    when a metric with `record` has no such row; any other metric at its last step. Its rows go by task, the metrics
    without a task first, and `better` maps a metric name to `lower` or `higher`, which colours the changes of that
    metric. `areas` maps a run id to its area report, {stage, step, source_file, rows: [[instance, depth, area]]};
    `step_names` maps a step number to its name.
    """
    groups: dict[tuple, list[Row]] = {}
    for r in runs:
        groups.setdefault((r.get("label"), r.get("source")), []).append(r)
    picks = {pick(g)["run_id"] for g in groups.values()}
    named = tick is not None
    if tick is None:
        done = sorted((r for r in runs if r.get("phase") == "done"), key=lambda r: (r.get("started") or 0, r["run_id"]),
                      reverse=True)
        newest: dict[Any, str] = {}
        for r in done:
            if r.get("source") == done[0].get("source") and len(newest) < 2:
                newest.setdefault(r.get("label"), r["run_id"])
        tick = list(newest.values())[::-1]
    enriched = [{**r, "state": state_of(r), "core-h": round(cost(r), 2), "pick": int(r["run_id"] in picks)}
                for r in order(runs)]
    script = f'<script src="{_h(plotly_src)}"></script>' if plotly_src else ""
    return _COMPARE.substitute(plotly=script, runs=_json_block(enriched), parameters=_json_block(parameters),
                               metrics=_json_block(metrics), areas=_json_block(areas or {}),
                               steps=_json_block({str(k): v for k, v in (step_names or {}).items()}),
                               tick=_json_block({"runs": tick, "named": named}), better=_json_block(better or {}))


# templates

_CSS = """
body{font:15px system-ui,sans-serif;margin:12px;background:#fff;color:#111}
table{border-collapse:collapse;width:100%;margin-bottom:16px}
td,th{padding:6px 4px;border-bottom:1px solid #ddd;text-align:left;font-size:14px;vertical-align:top}
small{color:#777;overflow-wrap:anywhere}h3{margin:16px 0 6px}
.st{font-weight:600}.s-running .st{color:#2a7}.s-stale .st{color:#e90}.s-dead .st,.s-failed .st,.s-hung .st{color:#d33}
.s-done .st{color:#888}.s-incomplete .st,.s-over_budget .st,.s-looping .st{color:#a3c}.s-stopped .st,.s-killed .st{color:#bbb}
select{font:inherit;margin:0 8px 8px 0}.hc{margin:0 0 10px}.hc svg{max-width:100%;height:auto}.worse{color:#d33}.better{color:#2a7}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}td,th{border-color:#333}small{color:#999}}
"""

_STATUS = Template("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>edr status</title>
<style>""" + _CSS + """</style></head><body>
<h3>runs ($n), $written</h3>
<table><tr><th>state</th><th>label</th><th>host</th><th>phase</th><th>age</th><th>batch</th></tr>$runs</table>
<h3>events</h3>
<table><tr><th>time</th><th>actor</th><th>run</th><th>kind</th><th>text</th></tr>$events</table>
<h3>hosts</h3>
<table>$hosts</table>
$charts
</body></html>
""")

_COMPARE = Template("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>edr compare</title>
<style>""" + _CSS + """
input.f{width:100%;box-sizing:border-box;font:inherit;font-size:13px}.tw{overflow-x:auto}
button{font:inherit;margin:0 8px 8px 0}summary h3{display:inline}#cmp th,#cmp td:first-child{overflow-wrap:anywhere}
#cmp small{white-space:nowrap}tr.task td{font-weight:600;padding-top:12px;overflow-wrap:anywhere}</style>$plotly</head><body>
<script type="application/json" id="edr-runs">$runs</script>
<script type="application/json" id="edr-parameters">$parameters</script>
<script type="application/json" id="edr-metrics">$metrics</script>
<script type="application/json" id="edr-areas">$areas</script>
<script type="application/json" id="edr-steps">$steps</script>
<script type="application/json" id="edr-tick">$tick</script>
<script type="application/json" id="edr-better">$better</script>
<details id="rd"><summary><h3>runs</h3></summary>
<p><small>A filter keeps the rows whose cell holds its text; &gt;n and &lt;n compare numbers. Tick the runs to compare;
the first one ticked is the base of every percent. Open the page as compare.html#runs=label@source,label@source to tick
those runs.</small></p>
<button id="tickall">tick the shown runs</button><button id="untick">untick all</button>
<div class="tw"><table id="runs"></table></div></details>
<h3>compare</h3><small id="lost"></small><div class="tw"><table id="cmp"></table></div>
<div id="area"><h3>area delta</h3>A <select id="aa"></select> B <select id="ab"></select> depth <select id="ad"></select>
<div class="tw"><table id="areat"></table></div><small id="areas"></small></div>
<div id="plots">
<h3>metric over steps</h3><select id="traj"></select><div id="trajp"></div>
<h3>scatter</h3>x <select id="sx"></select> y <select id="sy"></select> colour <select id="sc"></select><div id="scatp"></div>
<div id="phases"><h3>power parts</h3><div id="phasep"></div></div>
<h3>parallel coordinates</h3><small>every shown run: the parameters that differ, and</small> <select id="pm"></select><div id="parp"></div>
</div>
<script>
const q = s => document.querySelector(s), get = id => JSON.parse(document.getElementById(id).textContent);
const RUNS = get('edr-runs'), PARAMETERS = get('edr-parameters'), METRICS = get('edr-metrics');
const AREAS = get('edr-areas'), STEPS = get('edr-steps'), TICKED = get('edr-tick'), BETTER = get('edr-better');
const PAR = {}, PKEYS = [];
for (const p of PARAMETERS) { (PAR[p.run_id] ??= {})[p.key] = p.value; if (!PKEYS.includes(p.key)) PKEYS.push(p.key); }
PKEYS.sort();
const mkey = m => m.name + (m.task ? '[' + m.task + ']' : '');
// A metric with `record` shows its row of record, and `missing` without one; any other metric its last step.
const FIN = {}, MKEYS = [], META = {};
for (const m of METRICS) {
  const k = mkey(m), s = m.step ?? 1e9, cur = (FIN[m.run_id] ??= {}), rank = m.record === 1 ? 2 : m.record === 0 ? 0 : 1;
  if (!MKEYS.includes(k)) MKEYS.push(k);
  META[k] = m;
  if (!(k in cur) || rank > cur[k].rank || (rank === cur[k].rank && s >= cur[k].step))
    cur[k] = { v: rank ? m.value : null, step: s, rank, at: !rank ? 'missing' : m.step == null ? '' : m.stage + ' ' + m.step };
}
// The metrics without a task first, then by task and name; the numbers in a name compare by value.
const nat = (a, b) => a.localeCompare(b, undefined, { numeric: true });
MKEYS.sort((a, b) => nat(META[a].task || '', META[b].task || '') || nat(META[a].name, META[b].name));
const num = v => { const n = Number(v); return v === null || v === undefined || v === '' || !isFinite(n) ? null : n; };
const val = (r, d) => d in (FIN[r.run_id] || {}) ? FIN[r.run_id][d].v
  : d in (PAR[r.run_id] || {}) ? (num(PAR[r.run_id][d]) ?? PAR[r.run_id][d]) : r[d] ?? null;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// A name may break after an underscore, so a narrow column wraps it there.
const brk = s => esc(s).replace(/_/g, '_<wbr>');
// The format of board.num: six significant digits, every digit before the point, and no exponent.
const fmt = v => typeof v !== 'number' ? String(v ?? '') : Math.abs(v) >= 999999.5 ? v.toFixed(0)
  : Math.abs(v) >= 1e-6 || !v ? String(+v.toPrecision(6))
  : v.toFixed(Math.min(100, 5 - Math.floor(Math.log10(Math.abs(v))))).replace(/0+$$/, '');
const pct = (v, b) => v == null || b == null || b === 0 || v * b < 0 ? '' : (v > b ? '+' : '') + ((v - b) / Math.abs(b) * 100).toFixed(1) + '%';
const HAVE_PLOTLY = typeof Plotly !== 'undefined';
const DARK = matchMedia('(prefers-color-scheme: dark)').matches;
const LAYOUT = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: DARK ? '#eee' : '#111' },
  margin: { t: 30, r: 10, b: 40, l: 50 }, height: 320 };
const tr = (cells, tag = 'td') => '<tr>' + cells.map(c => '<' + tag + '>' + c + '</' + tag + '>').join('') + '</tr>';

const COLS = ['label', 'source', 'batch', 'host', 'state', 'phase', 'core-h', ...PKEYS];
const cell = (r, c) => PKEYS.includes(c) && !(c in r) ? PAR[r.run_id]?.[c] : r[c];
// The runs of the URL hash #runs=<run id or label@source>,..., else the runs the page was written with.
const WANT = (new URLSearchParams(location.hash.slice(1)).get('runs') || '').split(',').filter(Boolean);
const runOf = w => RUNS.find(r => r.run_id === w || r.pick && r.label + '@' + r.source === w);
const LOST = WANT.filter(w => !runOf(w));
const TICK = new Set(WANT.length ? WANT.map(runOf).filter(Boolean).map(r => r.run_id) : TICKED.runs);
const FILTER = {};
function keep(r) {
  return COLS.every(c => {
    const f = (FILTER[c] || '').trim(), v = cell(r, c);
    if (!f) return true;
    const m = f.match(/^([<>])\\s*(-?[0-9.eE+-]+)$$/);
    if (m && num(v) !== null) return m[1] === '>' ? num(v) > Number(m[2]) : num(v) < Number(m[2]);
    return String(v ?? '').toLowerCase().includes(f.toLowerCase());
  });
}
const shown = () => RUNS.filter(keep);
// In the order they were ticked, so the first run ticked is the base of the compare table.
const checked = () => [...TICK].map(id => RUNS.find(r => r.run_id === id)).filter(Boolean);
function runsBody() {
  return shown().map(r => tr(['<input type="checkbox" data-run="' + esc(r.run_id) + '"' + (TICK.has(r.run_id) ? ' checked' : '') + '>',
    ...COLS.map((c, i) => i ? esc(cell(r, c)) : '<span title="' + esc(r.run_id) + '">' + esc(r.label) + '</span>'
      + (r.retired ? ' <small>retired</small>' : ''))])).join('');
}
function runsTable() {
  const head = tr(['', ...COLS.map(c => esc(c))], 'th')
    + tr(['', ...COLS.map(c => '<input class="f" data-col="' + esc(c) + '" placeholder="filter">')], 'th');
  q('#runs').innerHTML = '<thead>' + head + '</thead><tbody id="rb">' + runsBody() + '</tbody>';
  q('#runs').addEventListener('input', e => { if (e.target.dataset?.col) { FILTER[e.target.dataset.col] = e.target.value;
    document.getElementById('rb').innerHTML = runsBody(); draw(); } });
  q('#runs').addEventListener('change', e => { const id = e.target.dataset?.run; if (!id) return;
    e.target.checked ? TICK.add(id) : TICK.delete(id); draw(); });
  q('#tickall').addEventListener('click', () => { shown().forEach(r => TICK.add(r.run_id));
    document.getElementById('rb').innerHTML = runsBody(); draw(); });
  q('#untick').addEventListener('click', () => { TICK.clear(); document.getElementById('rb').innerHTML = runsBody(); draw(); });
}
const DIR = { lower: -1, higher: 1 };
function compareTable(sel) {
  if (!sel.length) { q('#cmp').innerHTML = tr(['tick a run']); return; }
  const base = FIN[sel[0].run_id] || {}, rows = [];
  let task;
  for (const k of MKEYS.filter(k => sel.some(r => FIN[r.run_id]?.[k] != null))) {
    const m = META[k], dir = DIR[BETTER[m.name]];
    if ((m.task || '') !== task) {
      task = m.task || '';
      rows.push('<tr class="task"><td colspan="' + (sel.length + 1) + '">' + brk(task || 'flow') + '</td></tr>');
    }
    rows.push(tr([brk(m.name) + (m.unit ? ' <small>' + esc(m.unit) + '</small>' : ''), ...sel.map((r, i) => {
      const f = FIN[r.run_id]?.[k], v = f?.v, b = base[k]?.v;
      // The percent against the first run; across a sign change or from 0, the difference.
      const p = !i || v == null || b == null ? '' : pct(v, b) || (v === b ? '' : (v > b ? '+' : '') + fmt(v - b));
      const c = p && dir && v !== b ? ' class="' + ((v - b) * dir > 0 ? 'better' : 'worse') + '"' : '';
      return fmt(v) + (f?.at ? ' <small>' + esc(f.at) + '</small>' : '') + (p ? ' <small' + c + '>' + p + '</small>' : '');
    })]));
  }
  q('#cmp').innerHTML = tr(['metric', ...sel.map(r => brk(r.label) + '<wbr><small>@' + esc(r.source) + '</small>')], 'th')
    + rows.join('');
}
const ARUNS = RUNS.filter(r => r.run_id in AREAS);
function areaTable() {
  const a = AREAS[q('#aa').value], b = AREAS[q('#ab').value], d = Number(q('#ad').value);
  if (!a || !b) { q('#areat').innerHTML = ''; return; }
  const at = d => Object.fromEntries(d.rows.map(x => [x[0], x])), A = at(a), B = at(b);
  const names = [...new Set([...Object.keys(A), ...Object.keys(B)])].filter(n => (A[n] || B[n])[1] === d)
    .sort((x, y) => (A[y]?.[2] ?? 0) - (A[x]?.[2] ?? 0));
  const line = (n, va, vb) => tr([esc(n), fmt(va), fmt(vb), va == null || vb == null ? '' : fmt(vb - va), pct(vb, va)]);
  const top = x => x.rows.find(r => r[1] === 0)?.[2];
  q('#areat').innerHTML = tr(['instance', 'A', 'B', 'Δ', 'Δ %'], 'th') + names.map(n => line(n, A[n]?.[2], B[n]?.[2])).join('')
    + line('<top>', top(a), top(b));
  q('#areas').textContent = 'A: ' + a.stage + ' step ' + a.step + ', ' + a.source_file + '; B: ' + b.stage + ' step ' + b.step + ', ' + b.source_file;
}
function fill(id, opts, pick, labels) {
  q(id).innerHTML = opts.map((o, i) => '<option value="' + esc(o) + '"' + (o === pick ? ' selected' : '') + '>' + esc(labels ? labels[i] : o) + '</option>').join('');
  q(id).value = pick ?? opts[0] ?? '';
}
function parcoords(runs) {
  const mk = q('#pm').value, rs = runs.filter(r => num(FIN[r.run_id]?.[mk]?.v) !== null);
  const dims = [];
  for (const k of PKEYS) {
    const vs = rs.map(r => PAR[r.run_id]?.[k] ?? '');
    const uniq = [...new Set(vs)];
    if (uniq.length < 2) continue;
    if (vs.every(v => num(v) !== null)) { dims.push({ label: k, values: vs.map(Number) }); continue; }
    uniq.sort();
    dims.push({ label: k, values: vs.map(v => uniq.indexOf(v)), tickvals: uniq.map((_, i) => i), ticktext: uniq });
  }
  const mv = rs.map(r => FIN[r.run_id][mk].v);
  dims.push({ label: mk, values: mv });
  Plotly.react('parp', [{ type: 'parcoords', line: { color: mv, colorscale: 'Viridis', showscale: true }, dimensions: dims }],
    { ...LAYOUT, height: 380, margin: { t: 50, r: 60, b: 30, l: 60 } });
}
function plots(sel) {
  const tk = q('#traj').value, steps = [];
  Plotly.react('trajp', sel.map(r => {
    const pts = METRICS.filter(m => m.run_id === r.run_id && mkey(m) === tk && m.step != null).sort((a, b) => a.step - b.step);
    steps.push(...pts.map(p => p.step));
    return { x: pts.map(p => p.step), y: pts.map(p => p.value), name: r.label, mode: 'lines+markers' };
  }), { ...LAYOUT, title: tk, xaxis: { title: 'step', tickvals: [...new Set(steps)], ticktext: [...new Set(steps)].map(s => STEPS[s] ?? s) } });
  const sx = q('#sx').value, sy = q('#sy').value, sc = q('#sc').value, groups = {};
  for (const r of sel) (groups[fmt(val(r, sc))] ??= []).push(r);
  Plotly.react('scatp', Object.entries(groups).map(([g, rs]) => ({ x: rs.map(r => val(r, sx)), y: rs.map(r => val(r, sy)),
    text: rs.map(r => r.label), name: sc + '=' + g, mode: 'markers', marker: { size: 10 } })),
    { ...LAYOUT, xaxis: { title: sx }, yaxis: { title: sy } });
  const pk = MKEYS.filter(k => /^power__(?!total)/.test(META[k].canonical || ''));
  q('#phases').hidden = !pk.length;
  if (pk.length) {
    const tasks = [...new Set(pk.map(k => k.includes('[') ? k.slice(k.indexOf('[')) : ''))];
    const phases = [...new Set(pk.map(k => k.split('[')[0]))];
    Plotly.react('phasep', phases.map(p => ({ type: 'bar', name: p, x: sel.flatMap(r => tasks.map(t => r.label + t)),
      y: sel.flatMap(r => tasks.map(t => FIN[r.run_id]?.[p + t]?.v ?? 0)) })), { ...LAYOUT, barmode: 'stack' });
  }
  parcoords(shown());
}
function draw() {
  const sel = checked();
  compareTable(sel);
  if (HAVE_PLOTLY) plots(sel);
}
runsTable();
q('#rd').open = !(WANT.length || TICKED.named);
q('#lost').textContent = LOST.length ? 'no run is named ' + LOST.join(', ') : '';
const dims = [...PKEYS, ...MKEYS];
fill('#traj', MKEYS.filter(k => METRICS.some(m => mkey(m) === k && m.step != null)), null);
fill('#sx', dims, dims[0]); fill('#sy', dims, dims[1] ?? dims[0]); fill('#sc', ['label', 'host', 'source', ...PKEYS], 'label');
fill('#pm', MKEYS, MKEYS.find(k => !k.includes('[')) ?? MKEYS[0]);
// The area delta opens on the first two ticked runs with an area report.
const aids = ARUNS.map(r => r.run_id), alab = ARUNS.map(r => r.label + '@' + r.source);
const ta = [...checked().map(r => r.run_id).filter(id => id in AREAS), ...aids];
fill('#aa', aids, ta[0], alab); fill('#ab', aids, ta.find(id => id !== ta[0]) ?? ta[0], alab);
const depths = [...new Set(Object.values(AREAS).flatMap(a => a.rows.map(r => r[1])))].filter(d => d > 0).sort((a, b) => a - b);
fill('#ad', depths.map(String), String(depths[0] ?? 1));
q('#area').hidden = !ARUNS.length;
for (const id of ['#traj', '#sx', '#sy', '#sc', '#pm']) q(id).addEventListener('change', draw);
for (const id of ['#aa', '#ab', '#ad']) q(id).addEventListener('change', areaTable);
q('#plots').hidden = !HAVE_PLOTLY;
areaTable();
draw();
</script></body></html>
""")

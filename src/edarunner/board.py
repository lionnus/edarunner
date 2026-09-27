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
import sys
import time
from collections import Counter
from collections.abc import Iterable
from string import Template
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from .model import Marks

if TYPE_CHECKING:
    from .hosts import HostProbe

Row = dict[str, Any]

# compare.html loads Plotly from this file in data/board when a user put a copy there, else from the CDN.
PLOTLY_FILE = "plotly.min.js"
PLOTLY_URL = "https://cdn.plot.ly/plotly-2.35.2.min.js"
TERMINAL = ("done", "INCOMPLETE", "FAILED", "OVER_BUDGET", "STOPPED", "KILLED")
# Sort rank on a board; the live rows go before the finished ones.
RANK = {"dead": 0, "failed": 0, "hung": 1, "incomplete": 1, "looping": 2, "over_budget": 3,
         "host_full": 4, "killed": 4, "superseded": 5, "stopped": 5, "stale": 6, "running": 8, "done": 9}
_SHORT = {"running": "RUN", "dead": "DEAD", "hung": "HUNG", "looping": "LOOP", "over_budget": "OVER",
          "host_full": "FULL", "superseded": "SUPER", "incomplete": "INC", "failed": "FAIL", "stopped": "STOP",
          "killed": "KILL"}
STYLE = {"running": "green", "queued": "cyan", "stale": "yellow", "host_full": "yellow", "superseded": "yellow",
         "dead": "red", "hung": "red", "looping": "red", "over_budget": "red", "orphan": "red", "failed": "red",
         "incomplete": "magenta", "done": "dim", "retired": "dim", "stopped": "dim", "killed": "dim",
         "imported": "dim", "abandoned": "dim", "resumed": "cyan"}
# Resource marks from green to red, then black for a host that did not answer.
RESOURCE_MARKS = ("🟢", "🟡", "🟠", "🔴")
NO_ANSWER = "⚫"
SEVERITY = (NO_ANSWER, *reversed(RESOURCE_MARKS))
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


def resource_mark(used_fraction: float, thresholds: list[float]) -> str:
    """The mark of a resource: green, then yellow, orange and red from each threshold on."""
    return RESOURCE_MARKS[sum(used_fraction >= t for t in thresholds)]


def host_marks(probe: HostProbe | None, marks: Marks) -> dict[str, str]:
    """One mark each for cores, ram, scratch and gpu; gpu is '-' without GPUs, all black for None (no answer)."""
    if probe is None:
        return dict.fromkeys(("cores", "ram", "scratch", "gpu"), NO_ANSWER)
    p = probe

    def frac(used: float, total: float) -> float:
        return used / total if total else 0.0

    return {
        "cores": resource_mark(frac(p.load, p.cores), marks.cores),
        "ram": resource_mark(frac(p.total_ram_gb - p.free_ram_gb, p.total_ram_gb), marks.ram),
        "scratch": resource_mark(frac(p.total_gb - p.free_gb, p.total_gb), marks.scratch),
        "gpu": resource_mark(frac(p.gpus - p.gpus_idle, p.gpus), marks.gpu) if p.gpus else "-",
    }


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


def worst_mark(marks: Iterable[str]) -> str:
    """The most severe of `marks` by SEVERITY; '-' counts as green."""
    return min((m for m in marks if m in SEVERITY), key=SEVERITY.index, default=RESOURCE_MARKS[0])


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
    """Core hours: elapsed hours times `row['cores']`, or times 1 when the row has no cores."""
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


def _ts(t: float | None) -> str:
    return "-" if not t else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _stage_step(row: Row) -> str:
    stage = _s(row.get("stage"))
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

def narrow(rows: list[Row], width: int = 48, now: float | None = None) -> str:
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
        lines.append(f"    {_s(r.get('batch'))[-12:]:<12} {hm(_age_s(r, now)):>4} ago {_stage_step(r)[:10]:<10} "
                     f"{_fd(r)}"[:width])
    if not live:
        lines.append("nothing live")
    return "\n".join(lines)


def narrow_text(rows: list[Row], width: int = 48, now: float | None = None) -> Text:
    """The narrow board with the state of each run in its colour."""
    text = Text(narrow(rows, width, now))
    for m in re.finditer(r"(?m)^ *#\d+ (\S+)", text.plain):
        state = next((s for s in STYLE if _SHORT.get(s, s)[:5] == m.group(1)), None)
        if state:
            text.stylize(STYLE[state], m.start(1), m.end(1))
    return text


def handle(row: Row) -> str:
    """label@batch."""
    return f"{_s(row.get('label'))}@{_s(row.get('batch'))}"


def cols(head: list[str], body: list[list[Any]], width: int = 40) -> str:
    """Plain columns in `width`: the first left-aligned and cut to fit, the rest right-aligned. None prints as '-'."""
    rows = [head] + [["-" if c is None else _s(c) for c in r] for r in body]
    w = [max(len(r[i]) for r in rows) for i in range(len(head))]
    gap = " " * (2 if sum(w) + 2 * (len(w) - 1) <= width else 1)
    w[0] = max(1, min(w[0], width - sum(w[1:]) - len(gap) * (len(w) - 1)))
    return "\n".join(gap.join([r[0][:w[0]].ljust(w[0]), *(c.rjust(x) for c, x in zip(r[1:], w[1:]))])[:width].rstrip()
                     for r in rows)


STOP_FLAGS = {"hung": "--why hung", "looping": "--why looping", "over_budget": "--why over-budget",
              "host_full": "--now --why host-full", "superseded": "--after-task --why superseded"}


def triage_cmd(row: Row, state: str, hb: dict) -> str | None:
    """The one command a person runs next for a run in `state`; None for a running run or an orphan."""
    h = handle(row)
    if state in ("running", "orphan", "retired", "abandoned"):
        return None
    if state == "queued":
        return f"edr launch {row['batch']} --only {row['label']}"
    if state == "stale":
        return f"edr status {h} --live"
    if state == "dead":
        return f"edr continue {h} --stage {hb.get('stage') or row.get('stage')}" + (
            f" --from {hb['step_name']}" if hb.get("step_name") else "")
    if state in STOP_FLAGS:
        return f"edr stop {h} {STOP_FLAGS[state]}"
    if state == "done":
        return f"edr export --design {row.get('src')} --out exports/{row.get('src')}"
    return f"edr retire {h} --why {state}"


def wide(rows: list[Row], now: float | None = None) -> Table | str:
    """One line per run, every state, in board order."""
    now = now or time.time()
    body = [[f"#{n}", r.get("label"), r.get("host"), state_text(state_of(r)), r.get("phase"), _stage_step(r),
             hm(_age_s(r, now)), _fd(r), f"{cost(r, now):.1f}"] for n, r in enumerate(order(rows), 1)]
    if not body:
        return "no runs"
    return table(["#", "label", "host", "state", "phase", "stage/step", "age", "fail/done", "cost"], body,
                 styles={"label": "bold", "age": "dim"}, right=("age", "fail/done", "cost"))


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
               gate: str | None = None, samples: list[Row] | None = None) -> Group:
    """One run: identity, state, what its tool gate waits for, counts, stage rows, samples, metrics and the log tail."""
    now = now or time.time()
    ex = row.get("exit")
    parts: list[RenderableType] = [
        Text(_s(row.get("run_id")) or "?", style="bold"),
        Text.assemble(state_text(state_of(row)), f"  {_s(row.get('phase')) or '-'}  {_stage_step(row)}  "
                      f"exit {'-' if ex is None else ex}"),
        Text(f"label {_s(row.get('label'))}  config {_s(row.get('config'))}  batch {_s(row.get('batch'))}  "
             f"src {_s(row.get('src'))}{' dirty' if row.get('dirty') else ''}"),
        Text.assemble(f"host {_s(row.get('host'))}  root ", (_s(row.get("root")), "dim")),
        Text.assemble("started ", (_ts(row.get("started")), "dim"), "  updated ", (_ts(row.get("updated")), "dim"),
                      f" ({hm(_age_s(row, now))} ago)  cost {cost(row, now):.1f} core-h"),
        Text("counts " + (" ".join(f"{k} {v}" for k, v in _counts(row).items()) or "-")),
        Text(f"disk free {_s(row.get('disk_free_gb')) or '-'} GB  tree {_s(row.get('tree_gb')) or '-'} GB"),
    ]
    if gate and is_live(row):
        parts.insert(2, Text(f"gate waits for {gate}", style="yellow"))
    if row.get("killed_by"):
        parts.append(Text(f"killed by {row['killed_by']}", style="red"))
    if stage_rows:
        parts += [Text(""), Text("stages", style="bold"), table(
            ["stage", "task", "attempt", "status", "exit", "started", "ended", "signature"],
            [[s.get("stage"), s.get("task"), s.get("attempt"), state_text(s.get("status")), s.get("exit"),
              _ts(s.get("started")), _ts(s.get("ended")), s.get("signature")] for s in stage_rows],
            styles={"started": "dim", "ended": "dim"})]
    sample_tab = samples_table(samples or [])
    if sample_tab is not None:
        first, last = _ts(samples[0]["ts"]), _ts(samples[-1]["ts"])
        parts += [Text(""), Text(f"samples {first} to {last}, from run_samples", style="bold"), sample_tab]
    if metrics:
        parts += [Text(""), Text("metrics", style="bold"), table(
            ["stage", "step", "task", "name", "value", "unit"],
            [[m.get("stage"), m.get("step"), m.get("task"), m.get("canonical") or m.get("name"), m.get("value"),
              m.get("unit")] for m in metrics], right=("step", "value"))]
    if tail:
        parts += [Text(""), Text("log tail", style="bold"), Text(tail.rstrip())]
    return Group(*parts)


# html pages

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
                 areas: dict[str, Row] | None = None, step_names: dict[int, str] | None = None) -> str:
    """A self-contained page over the lists; the plots need `plotly_src`, the tables do not.

    `areas` maps a run id to its last area report, {stage, step, source_file, rows: [[instance, depth, area]]};
    `step_names` maps a step number to its name.
    """
    enriched = [{**r, "state": state_of(r), "cost": round(cost(r), 2)} for r in order(runs)]
    script = f'<script src="{_h(plotly_src)}"></script>' if plotly_src else ""
    return _COMPARE.substitute(plotly=script, runs=_json_block(enriched), parameters=_json_block(parameters),
                               metrics=_json_block(metrics), areas=_json_block(areas or {}),
                               steps=_json_block({str(k): v for k, v in (step_names or {}).items()}))


# templates

_CSS = """
body{font:15px system-ui,sans-serif;margin:12px;background:#fff;color:#111}
table{border-collapse:collapse;width:100%;margin-bottom:16px}
td,th{padding:6px 4px;border-bottom:1px solid #ddd;text-align:left;font-size:14px;vertical-align:top}
small{color:#777;word-break:break-all}h3{margin:16px 0 6px}
.st{font-weight:600}.s-running .st{color:#2a7}.s-stale .st{color:#e90}.s-dead .st,.s-failed .st,.s-hung .st{color:#d33}
.s-done .st{color:#888}.s-incomplete .st,.s-over_budget .st,.s-looping .st{color:#a3c}.s-stopped .st,.s-killed .st{color:#bbb}
select{font:inherit;margin:0 8px 8px 0}.hc{margin:0 0 10px}.hc svg{max-width:100%;height:auto}.up{color:#d33}.dn{color:#2a7}
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
button{font:inherit;margin:0 8px 8px 0}</style>$plotly</head><body>
<script type="application/json" id="edr-runs">$runs</script>
<script type="application/json" id="edr-parameters">$parameters</script>
<script type="application/json" id="edr-metrics">$metrics</script>
<script type="application/json" id="edr-areas">$areas</script>
<script type="application/json" id="edr-steps">$steps</script>
<h3>runs</h3>
<p><small>A filter keeps the rows whose cell holds its text; &gt;n and &lt;n compare numbers. Tick the runs to compare.</small></p>
<button id="tickall">tick the shown runs</button><button id="untick">untick all</button>
<div class="tw"><table id="runs"></table></div>
<h3>compare</h3><div class="tw"><table id="cmp"></table></div>
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
const AREAS = get('edr-areas'), STEPS = get('edr-steps');
const PAR = {}, PKEYS = [];
for (const p of PARAMETERS) { (PAR[p.run_id] ??= {})[p.key] = p.value; if (!PKEYS.includes(p.key)) PKEYS.push(p.key); }
PKEYS.sort();
const mkey = m => (m.canonical || m.name) + (m.task ? '[' + m.task + ']' : '');
const FIN = {}, MKEYS = [];
for (const m of METRICS) {
  const k = mkey(m), s = m.step ?? 1e9, cur = (FIN[m.run_id] ??= {});
  if (!MKEYS.includes(k)) MKEYS.push(k);
  if (!(k in cur) || s >= cur[k].step) cur[k] = { v: m.value, step: s };
}
MKEYS.sort();
const num = v => { const n = Number(v); return v === null || v === undefined || v === '' || !isFinite(n) ? null : n; };
const val = (r, d) => d in (FIN[r.run_id] || {}) ? FIN[r.run_id][d].v
  : d in (PAR[r.run_id] || {}) ? (num(PAR[r.run_id][d]) ?? PAR[r.run_id][d]) : r[d] ?? null;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = v => v == null ? '' : typeof v === 'number' && !Number.isInteger(v)
  ? (Math.abs(v) >= 1000 ? v.toFixed(1) : v.toPrecision(4)) : String(v);
const pct = (v, b) => v == null || b == null || b === 0 || v * b < 0 ? '' : (v > b ? '+' : '') + ((v - b) / Math.abs(b) * 100).toFixed(1) + '%';
const HAVE_PLOTLY = typeof Plotly !== 'undefined';
const DARK = matchMedia('(prefers-color-scheme: dark)').matches;
const LAYOUT = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: DARK ? '#eee' : '#111' },
  margin: { t: 30, r: 10, b: 40, l: 50 }, height: 320 };
const tr = (cells, tag = 'td') => '<tr>' + cells.map(c => '<' + tag + '>' + c + '</' + tag + '>').join('') + '</tr>';

const COLS = ['label', 'src', 'batch', 'host', 'state', 'phase', 'cost', ...PKEYS];
const cell = (r, c) => PKEYS.includes(c) && !(c in r) ? PAR[r.run_id]?.[c] : r[c];
const TICK = new Set(RUNS.slice(0, 2).map(r => r.run_id));
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
const checked = () => RUNS.filter(r => TICK.has(r.run_id));
function runsBody() {
  return shown().map(r => tr(['<input type="checkbox" data-run="' + esc(r.run_id) + '"' + (TICK.has(r.run_id) ? ' checked' : '') + '>',
    ...COLS.map((c, i) => i ? esc(cell(r, c)) : '<span title="' + esc(r.run_id) + '">' + esc(r.label) + '</span>')])).join('');
}
function runsTable() {
  const head = tr(['', ...COLS.map(c => esc(c === 'src' ? 'design' : c))], 'th')
    + tr(['', ...COLS.map(c => '<input class="f" data-col="' + esc(c) + '" placeholder="filter">')], 'th');
  q('#runs').innerHTML = '<thead>' + head + '</thead><tbody id="rb">' + runsBody() + '</tbody>';
  q('#runs').addEventListener('input', e => { if (e.target.dataset?.col) { FILTER[e.target.dataset.col] = e.target.value;
    document.getElementById('rb').innerHTML = runsBody(); draw(); } });
  q('#runs').addEventListener('change', e => { const id = e.target.dataset?.run; if (!id) return;
    e.target.checked ? TICK.add(id) : TICK.delete(id); draw(); });
  q('#tickall').addEventListener('click', () => { shown().forEach(r => TICK.add(r.run_id)); runsTable(); draw(); });
  q('#untick').addEventListener('click', () => { TICK.clear(); runsTable(); draw(); });
}
function compareTable(sel) {
  if (!sel.length) { q('#cmp').innerHTML = tr(['tick a run']); return; }
  const base = FIN[sel[0].run_id] || {};
  const rows = MKEYS.filter(k => sel.some(r => FIN[r.run_id]?.[k] != null)).map(k => tr([esc(k), ...sel.map((r, i) => {
    const v = FIN[r.run_id]?.[k]?.v, b = base[k]?.v, p = i ? pct(v, b) : '';
    return fmt(v) + (p ? ' <small class="' + (v > b ? 'up' : 'dn') + '">' + p + '</small>' : '');
  })]));
  q('#cmp').innerHTML = tr(['metric', ...sel.map(r => esc(r.label))], 'th') + rows.join('');
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
  const pk = MKEYS.filter(k => k.startsWith('power__') && !k.startsWith('power__total'));
  q('#phases').hidden = !pk.length;
  if (pk.length) {
    const tasks = [...new Set(pk.map(k => k.includes('[') ? k.slice(k.indexOf('[')) : ''))];
    const phases = [...new Set(pk.map(k => k.slice(7).split('[')[0]))];
    Plotly.react('phasep', phases.map(p => ({ type: 'bar', name: p, x: sel.flatMap(r => tasks.map(t => r.label + t)),
      y: sel.flatMap(r => tasks.map(t => FIN[r.run_id]?.['power__' + p + t]?.v ?? 0)) })), { ...LAYOUT, barmode: 'stack' });
  }
  parcoords(shown());
}
function draw() {
  const sel = checked();
  compareTable(sel);
  if (HAVE_PLOTLY) plots(sel);
}
runsTable();
const dims = [...PKEYS, ...MKEYS];
fill('#traj', MKEYS.filter(k => METRICS.some(m => mkey(m) === k && m.step != null)), null);
fill('#sx', dims, dims[0]); fill('#sy', dims, dims[1] ?? dims[0]); fill('#sc', ['label', 'host', 'src', ...PKEYS], 'label');
fill('#pm', MKEYS, MKEYS.find(k => !k.includes('[')) ?? MKEYS[0]);
const aids = ARUNS.map(r => r.run_id), alab = ARUNS.map(r => r.label + ' ' + r.src);
fill('#aa', aids, aids[0], alab); fill('#ab', aids, aids[1] ?? aids[0], alab);
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

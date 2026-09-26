"""Boards: 48-column text, the wide table, one run's detail, status.html and compare.html.

See docs/design.md sections 7 and 10. A row is a `runs` row of the ledger;
`counts` may be a dict or JSON text.
"""

from __future__ import annotations

import html
import json
import os
import time
import urllib.request
from collections import Counter
from pathlib import Path
from string import Template
from typing import Any

Row = dict[str, Any]

PLOTLY_URL = "https://cdn.plot.ly/plotly-2.35.2.min.js"
TERMINAL = ("done", "INCOMPLETE", "FAILED", "OVER_BUDGET", "STOPPED", "KILLED")
# Sort rank on a board; the live rows go before the finished ones.
_RANK = {"dead": 0, "failed": 0, "hung": 1, "incomplete": 1, "looping": 2, "over_budget": 3,
         "host_full": 4, "killed": 4, "superseded": 5, "stopped": 5, "stale": 6, "running": 8, "done": 9}
_SHORT = {"running": "RUN", "dead": "DEAD", "hung": "HUNG", "looping": "LOOP", "over_budget": "OVER",
          "host_full": "FULL", "superseded": "SUPER", "incomplete": "INC", "failed": "FAIL", "stopped": "STOP",
          "killed": "KILL"}


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
    return sorted(rows, key=lambda r: (not is_live(r), _RANK.get(state_of(r), 7), _s(r.get("label")), _s(r.get("run_id"))))


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


def _table(head: list[str], body: list[list[Any]]) -> str:
    rows = [head, *[[_s(c) for c in r] for r in body]]
    w = [max(len(r[i]) for r in rows) for i in range(len(head))]
    return "\n".join(" ".join(f"{c:<{w[i]}}" for i, c in enumerate(r)).rstrip() for r in rows)


# text boards

def narrow(rows: list[Row], width: int = 48, now: float | None = None) -> str:
    """Two lines per live run, dead first, in `width` columns. For a phone."""
    now = now or time.time()
    ordered = order(rows)
    live = [r for r in ordered if is_live(r)]
    counts = Counter(state_of(r) for r in ordered)
    head = time.strftime("%d.%m %H:%M", time.localtime(now)) + " " + " ".join(
        f"{_SHORT.get(k, k)}:{v}" for k, v in sorted(counts.items(), key=lambda kv: _RANK.get(kv[0], 7)))
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


def wide(rows: list[Row], now: float | None = None) -> str:
    """One line per run, every state, in board order."""
    now = now or time.time()
    body = [[f"#{n}", r.get("label"), r.get("host"), state_of(r), r.get("phase"), _stage_step(r),
             hm(_age_s(r, now)), _fd(r), f"{cost(r, now):.1f}"] for n, r in enumerate(order(rows), 1)]
    if not body:
        return "no runs"
    return _table(["#", "label", "host", "state", "phase", "stage/step", "age", "fail/done", "cost"], body)


def run_detail(row: Row, stage_rows: list[Row], metrics: list[Row], tail: str, now: float | None = None) -> str:
    """One run: identity, state, counts, stage rows, metrics and the log tail."""
    now = now or time.time()
    ex = row.get("exit")
    lines = [
        _s(row.get("run_id")) or "?",
        f"{state_of(row)}  {_s(row.get('phase')) or '-'}  {_stage_step(row)}  exit {'-' if ex is None else ex}",
        f"label {_s(row.get('label'))}  config {_s(row.get('config'))}  batch {_s(row.get('batch'))}  "
        f"src {_s(row.get('src'))}{' dirty' if row.get('dirty') else ''}",
        f"host {_s(row.get('host'))}  root {_s(row.get('root'))}",
        f"started {_ts(row.get('started'))}  updated {_ts(row.get('updated'))} ({hm(_age_s(row, now))} ago)  "
        f"cost {cost(row, now):.1f} core-h",
        "counts " + (" ".join(f"{k} {v}" for k, v in _counts(row).items()) or "-"),
        f"disk free {_s(row.get('disk_free_gb')) or '-'} GB  tree {_s(row.get('tree_gb')) or '-'} GB",
    ]
    if row.get("killed_by"):
        lines.append(f"killed by {row['killed_by']}")
    if stage_rows:
        lines += ["", "stages", _table(["stage", "task", "attempt", "status", "exit", "started", "ended", "signature"], [
            [s.get("stage"), s.get("task"), s.get("attempt"), s.get("status"), s.get("exit"),
             _ts(s.get("started")), _ts(s.get("ended")), s.get("signature")] for s in stage_rows])]
    if metrics:
        lines += ["", "metrics", _table(["stage", "step", "task", "name", "value", "unit"], [
            [m.get("stage"), m.get("step"), m.get("task"), m.get("canonical") or m.get("name"), m.get("value"),
             m.get("unit")] for m in metrics])]
    if tail:
        lines += ["", "log tail", tail.rstrip()]
    return "\n".join(lines)


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


def status_html(rows: list[Row], events: list[Row], hosts: dict[str, Any], now: float | None = None) -> str:
    """A phone-width page: every run in board order, the last 50 events, the hosts."""
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
    return _STATUS.substitute(written=_h(_ts(now)), n=len(rows), runs=run_rows, events=event_rows, hosts=host_rows)


def compare_html(runs: list[Row], params: list[Row], metrics: list[Row], plotly_src: str | None) -> str:
    """A self-contained page over the three lists; the plots need `plotly_src`, the tables do not."""
    enriched = [{**r, "state": state_of(r), "cost": round(cost(r), 2)} for r in order(runs)]
    script = f'<script src="{_h(plotly_src)}"></script>' if plotly_src else ""
    return _COMPARE.substitute(plotly=script, runs=_json_block(enriched), params=_json_block(params),
                               metrics=_json_block(metrics))


def ensure_plotly(data_dir: str | os.PathLike) -> Path | None:
    """Download plotly into `data_dir/board/` once; None when the download fails."""
    out = Path(data_dir) / "board" / PLOTLY_URL.rsplit("/", 1)[1]
    if out.is_file():
        return out
    tmp = out.with_name(out.name + ".tmp")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(PLOTLY_URL, timeout=60) as resp:
            tmp.write_bytes(resp.read())
        os.replace(tmp, out)
        return out
    except OSError:
        tmp.unlink(missing_ok=True)
        return None


# templates

_CSS = """
body{font:15px system-ui,sans-serif;margin:12px;background:#fff;color:#111}
table{border-collapse:collapse;width:100%;margin-bottom:16px}
td,th{padding:6px 4px;border-bottom:1px solid #ddd;text-align:left;font-size:14px;vertical-align:top}
small{color:#777;word-break:break-all}h3{margin:16px 0 6px}
.st{font-weight:600}.s-running .st{color:#2a7}.s-stale .st{color:#e90}.s-dead .st,.s-failed .st,.s-hung .st{color:#d33}
.s-done .st{color:#888}.s-incomplete .st,.s-over_budget .st,.s-looping .st{color:#a3c}.s-stopped .st,.s-killed .st{color:#bbb}
select{font:inherit;margin:0 8px 8px 0}.up{color:#d33}.dn{color:#2a7}
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
</body></html>
""")

_COMPARE = Template("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>edr compare</title>
<style>""" + _CSS + """</style>$plotly</head><body>
<script type="application/json" id="edr-runs">$runs</script>
<script type="application/json" id="edr-params">$params</script>
<script type="application/json" id="edr-metrics">$metrics</script>
<h3>runs</h3><table id="runs"></table>
<h3>compare</h3><table id="cmp"></table>
<div id="plots">
<h3>trajectory</h3><select id="traj"></select><div id="trajp"></div>
<h3>scatter</h3>x <select id="sx"></select> y <select id="sy"></select> colour <select id="sc"></select><div id="scatp"></div>
<div id="phases"><h3>phases</h3><div id="phasep"></div></div>
<h3>parallel coordinates</h3><div id="parp"></div>
</div>
<script>
const q = s => document.querySelector(s), get = id => JSON.parse(document.getElementById(id).textContent);
const RUNS = get('edr-runs'), PARAMS = get('edr-params'), METRICS = get('edr-metrics');
const PAR = {}, PKEYS = [];
for (const p of PARAMS) { (PAR[p.run_id] ??= {})[p.key] = p.value; if (!PKEYS.includes(p.key)) PKEYS.push(p.key); }
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
const fmt = v => v == null ? '' : typeof v === 'number' && !Number.isInteger(v) ? v.toPrecision(4) : String(v);
const checked = () => RUNS.filter(r => document.getElementById('c-' + r.run_id)?.checked);
const HAVE_PLOTLY = typeof Plotly !== 'undefined';
const DARK = matchMedia('(prefers-color-scheme: dark)').matches;
const LAYOUT = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: DARK ? '#eee' : '#111' },
  margin: { t: 30, r: 10, b: 40, l: 50 }, height: 320 };
const tr = (cells, tag = 'td') => '<tr>' + cells.map(c => '<' + tag + '>' + c + '</' + tag + '>').join('') + '</tr>';

function runsTable() {
  const rows = RUNS.map((r, i) => tr(['<input type="checkbox" id="c-' + esc(r.run_id) + '"' + (i < 2 ? ' checked' : '') + '>',
    '<span title="' + esc(r.run_id) + '">' + esc(r.label) + '</span>', esc(r.host), esc(r.state), esc(r.phase),
    ...PKEYS.map(k => esc(PAR[r.run_id]?.[k]))]));
  q('#runs').innerHTML = tr(['', 'label', 'host', 'state', 'phase', ...PKEYS].map(esc), 'th') + rows.join('');
  q('#runs').addEventListener('change', draw);
}
function compareTable(sel) {
  if (!sel.length) { q('#cmp').innerHTML = tr(['tick a run']); return; }
  const base = FIN[sel[0].run_id] || {};
  const rows = MKEYS.map(k => tr([esc(k), ...sel.map((r, i) => {
    const v = FIN[r.run_id]?.[k]?.v, b = base[k]?.v;
    if (!i || v == null || b == null || b === 0) return fmt(v);
    const p = (v - b) / Math.abs(b) * 100;
    return fmt(v) + ' <small class="' + (p > 0 ? 'up' : 'dn') + '">' + (p > 0 ? '+' : '') + p.toFixed(1) + '%</small>';
  })]));
  q('#cmp').innerHTML = tr(['metric', ...sel.map(r => esc(r.label))], 'th') + rows.join('');
}
function fill(id, opts, pick) {
  q(id).innerHTML = opts.map(o => '<option' + (o === pick ? ' selected' : '') + '>' + esc(o) + '</option>').join('');
  q(id).addEventListener('change', draw);
}
function plots(sel) {
  const tk = q('#traj').value;
  Plotly.react('trajp', sel.map(r => {
    const pts = METRICS.filter(m => m.run_id === r.run_id && mkey(m) === tk && m.step != null).sort((a, b) => a.step - b.step);
    return { x: pts.map(p => p.step), y: pts.map(p => p.value), name: r.label, mode: 'lines+markers' };
  }), { ...LAYOUT, title: tk, xaxis: { title: 'step' } });
  const sx = q('#sx').value, sy = q('#sy').value, sc = q('#sc').value, groups = {};
  for (const r of sel) (groups[fmt(val(r, sc))] ??= []).push(r);
  Plotly.react('scatp', Object.entries(groups).map(([g, rs]) => ({ x: rs.map(r => val(r, sx)), y: rs.map(r => val(r, sy)),
    text: rs.map(r => r.label), name: sc + '=' + g, mode: 'markers', marker: { size: 10 } })),
    { ...LAYOUT, xaxis: { title: sx }, yaxis: { title: sy } });
  const pk = MKEYS.filter(k => k.startsWith('power.') && !k.startsWith('power.total'));
  q('#phases').hidden = !pk.length;
  if (pk.length) {
    const tasks = [...new Set(pk.map(k => k.includes('[') ? k.slice(k.indexOf('[')) : ''))];
    const phases = [...new Set(pk.map(k => k.slice(6).split('[')[0]))];
    Plotly.react('phasep', phases.map(p => ({ type: 'bar', name: p, x: sel.flatMap(r => tasks.map(t => r.label + t)),
      y: sel.flatMap(r => tasks.map(t => FIN[r.run_id]?.['power.' + p + t]?.v ?? 0)) })), { ...LAYOUT, barmode: 'stack' });
  }
  const dims = [...PKEYS, ...MKEYS].map(d => ({ label: d, values: sel.map(r => num(val(r, d))) }))
    .filter(d => d.values.every(v => v !== null));
  if (dims.length) Plotly.react('parp', [{ type: 'parcoords', line: { color: sel.map((_, i) => i) }, dimensions: dims }],
    { ...LAYOUT, height: 360 });
}
function draw() { const sel = checked(); compareTable(sel); if (HAVE_PLOTLY) plots(sel); }
runsTable();
const dims = [...PKEYS, ...MKEYS];
fill('#traj', MKEYS.filter(k => METRICS.some(m => mkey(m) === k && m.step != null)), null);
fill('#sx', dims, dims[0]); fill('#sy', dims, dims[1] ?? dims[0]); fill('#sc', ['label', 'host', ...PKEYS], 'label');
q('#plots').hidden = !HAVE_PLOTLY;
draw();
</script></body></html>
""")

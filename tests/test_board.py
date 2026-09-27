"""Tests of edarunner.board on rows shaped like the ledger `runs` table."""

import json
import re
import shutil
import subprocess
import time

import pytest

from edarunner import board
from edarunner.ledger import Ledger

NOW = 1_800_000_000
SRC = "gaaa111"
RUN = {
    "dead": "20261002_1130_a_demo_" + SRC,
    "stale": "20261002_1130_b_nodw_demo_" + SRC,
    "run1": "20261002_1130_c_demo_" + SRC,
    "run2": "20261002_1130_d_demo_" + SRC,
    "done": "20261001_0900_a_demo_" + SRC,
    "fail": "20261001_0900_b_nodw_demo_" + SRC,
}


def _row(key, label, phase, state=None, age=10, **extra):
    row = {"run_id": RUN[key], "batch": "demo", "label": label, "config": "demo", "build_tag": "demo", "src": SRC,
           "dirty": 0, "host": "local", "root": "/tmp/edr-demo/x/edr/demo/" + RUN[key], "created": NOW - 7200,
           "phase": phase, "state": state, "stage": "synth", "step": 3, "exit": None, "killed_by": None,
           "started": NOW - 7200, "updated": NOW - age, "disk_free_gb": 100.0, "tree_gb": 1.5,
           "counts": json.dumps({"done": 1, "failed": 0, "skipped": 0, "running": 1, "queued": 0})}
    row.update(extra)
    return row


def _rows():
    return [
        _row("done", "a", "done", "running", age=3600, exit=0, counts={"done": 2, "failed": 0}),
        _row("run1", "c", "stage:synth", "running"),
        _row("fail", "b_nodw", "INCOMPLETE:1f0s", "running", age=7000, exit=8, stage="power", step=None,
             counts=json.dumps({"done": 1, "failed": 1})),
        _row("run2", "d" * 40, "group:power_" * 3, None, host="larain9-long-name", batch="iccd2026_g10_long"),
        _row("stale", "b_nodw", "stage:pnr", "stale", age=900),
        _row("dead", "a", "stage:synth", "dead", age=5000),
    ]


def test_order_and_state():
    ordered = [r["run_id"] for r in board.order(_rows())]
    assert ordered[:3] == [RUN["dead"], RUN["stale"], RUN["run1"]]
    assert ordered[3] == RUN["run2"]
    assert ordered[4:] == [RUN["fail"], RUN["done"]]
    assert board.state_of(_row("fail", "b", "INCOMPLETE:1f0s", "running")) == "incomplete"
    assert board.state_of(_row("run1", "c", None, None)) == "running"
    assert board.is_live(_row("done", "a", "KILLED:SIGTERM")) is False


@pytest.mark.parametrize("width", [48, 40])
def test_narrow_width_and_order(width):
    text = board.narrow(_rows(), width=width, now=NOW)
    lines = text.splitlines()
    assert all(len(line) <= width for line in lines), text
    assert lines[0].startswith(time.strftime("%d.%m %H:%M", time.localtime(NOW)) + " DEAD:1 INC:1")
    body = lines[2:]
    assert len(body) == 8, text
    assert body[0].startswith(" #1 DEAD  a") and body[2].startswith(" #2 stale b_nodw")
    assert body[4].startswith(" #3 RUN   c") and body[6].startswith(" #4 RUN   dddd")
    assert "INCOMPLETE" not in text and "nothing live" not in text
    if width == 48:
        assert lines[0].endswith(" DEAD:1 INC:1 stale:1 RUN:2 done:1")
        assert body[1] == "    demo           1h ago synth/3    0f/1d"
        assert body[7] == "    026_g10_long   0m ago synth/3    0f/1d"
    assert board.narrow([], now=NOW).splitlines()[-1] == "nothing live"
    assert board.narrow([_row("done", "a", "done")], now=NOW).endswith("nothing live")


def test_wide_all_states():
    text = board.plain(board.wide(_rows(), now=NOW))
    lines = text.splitlines()
    assert lines[0].split() == ["#", "label", "host", "state", "phase", "stage/step", "age", "fail/done", "cost"]
    assert len(lines) == 8 and "\x1b" not in text
    assert lines[2].startswith("#1  a") and " dead " in lines[2]
    assert lines[6].startswith("#5  b_nodw") and " incomplete " in lines[6] and " 1f/1d " in lines[6]
    assert lines[7].startswith("#6  a") and " done " in lines[7]
    assert board.wide([]) == "no runs"


def test_narrow_text_colours_the_state():
    text = board.narrow_text(_rows(), now=NOW)
    assert text.plain == board.narrow(_rows(), now=NOW)
    styled = {text.plain[s.start:s.end]: str(s.style) for s in text.spans}
    assert styled == {"DEAD": "red", "stale": "yellow", "RUN": "green"}


def test_plain_and_console_have_no_escape_codes(monkeypatch, capsys):
    table = board.wide(_rows(), now=NOW)
    assert "\x1b" not in board.plain(table)
    monkeypatch.setenv("NO_COLOR", "1")
    board.console().print(table)
    assert "\x1b" not in capsys.readouterr().out


def test_cost():
    live = _row("run1", "c", "stage:synth", "running", cores=4)
    assert board.cost(live, now=NOW) == pytest.approx(2.0 * 4)
    ended = _row("done", "a", "done", age=3600)
    assert board.cost(ended) == pytest.approx(1.0)
    assert board.cost({"run_id": "x", "phase": "done"}) == 0.0


def test_run_detail():
    row = _row("run1", "c", "stage:synth", "running", killed_by=None)
    stages = [{"stage": "synth", "task": "", "attempt": 1, "status": "running", "exit": None, "started": NOW - 7200,
               "ended": None, "signature": None},
              {"stage": "power", "task": "k_bad", "attempt": 1, "status": "failed", "exit": 1, "started": NOW - 60,
               "ended": NOW - 50, "signature": "boom: kernel bad failed"}]
    metrics = [{"stage": "synth", "step": 3, "task": "", "name": "area_cell_um2", "canonical": "area.cell",
                "value": 1031.5, "unit": "um2"}]
    text = board.plain(board.run_detail(row, stages, metrics, "line one\nline two\n", now=NOW))
    assert text.startswith(RUN["run1"] + "\nrunning  stage:synth  synth/3  exit -")
    assert "cost 2.0 core-h" in text and "counts done 1 failed 0" in text
    assert "k_bad" in text and "boom: kernel bad failed" in text
    assert "area.cell" in text and "1031.5" in text
    assert text.endswith("log tail\nline one\nline two")


def test_status_html():
    rows = _rows()
    events = [{"id": i, "ts": NOW - i, "actor": "watch", "run_id": RUN["dead"], "kind": "dead", "text": f"event {i}"}
              for i in range(60)]
    hosts = {"local": {"free_cores": 3, "free_ram_gb": 6.5}, "larain9": {"free_cores": 100, "free_gb": 700}}
    page = board.status_html(rows, events, hosts, now=NOW)
    assert 'name="viewport"' in page and "prefers-color-scheme:dark" in page
    for run_id in RUN.values():
        assert run_id in page
    assert page.index(RUN["dead"]) < page.index(RUN["stale"]) < page.index(RUN["run1"]) < page.index(RUN["done"])
    assert "event 59" in page and "event 10" in page and "event 9" not in page
    assert "<th>free_gb</th>" in page and "<td>larain9</td>" in page and "<td>6.5</td>" in page
    assert "&lt;" not in page.split("<body>")[0]
    assert board.status_html([], [], {}, now=NOW).count("<table>") == 3


def _json_blocks(page):
    return {m.group(1): json.loads(m.group(2)) for m in
            re.finditer(r'<script type="application/json" id="([^"]+)">(.*?)</script>', page, re.S)}


def _compare_input():
    rows = _rows()[:3]
    params = [{"run_id": r["run_id"], "key": k, "value": v, "source": "config"}
              for r in rows for k, v in (("DW", "0"), ("LANES", "8"), ("note", "a</script>b"))]
    metrics = []
    for i, r in enumerate(rows):
        metrics += [{"run_id": r["run_id"], "stage": "synth", "step": s, "task": "", "name": "area_cell_um2",
                     "canonical": "area.cell", "value": 1000 + 10 * s + i, "unit": "um2"} for s in (1, 2, 3)]
        metrics += [{"run_id": r["run_id"], "stage": "power", "step": None, "task": "k_small", "name": f"power_{ph}",
                     "canonical": f"power.{ph}", "value": 0.1 * (i + 1), "unit": "W"} for ph in ("A", "B", "total")]
    return rows, params, metrics


def test_compare_html_without_plotly():
    rows, params, metrics = _compare_input()
    page = board.compare_html(rows, params, metrics, None)
    assert "<script src=" not in page
    blocks = _json_blocks(page)
    assert set(blocks) == {"edr-runs", "edr-params", "edr-metrics"}
    assert [r["run_id"] for r in blocks["edr-runs"]] == [RUN["run1"], RUN["fail"], RUN["done"]]
    assert blocks["edr-runs"][1]["state"] == "incomplete" and "cost" in blocks["edr-runs"][0]
    assert blocks["edr-params"] == params and blocks["edr-metrics"] == metrics
    assert "</script>b" not in page.split("<h3>")[0].split("edr-params")[1]
    with_plotly = board.compare_html(rows, params, metrics, "plotly-2.35.2.min.js")
    assert '<script src="plotly-2.35.2.min.js"></script>' in with_plotly


_NODE_STUB = r"""
const fs = require('fs'); const page = fs.readFileSync(process.argv[1], 'utf8');
const blocks = {}; for (const m of page.matchAll(/<script type="application\/json" id="([^"]+)">([\s\S]*?)<\/script>/g)) blocks[m[1]] = m[2];
const els = {}; const el = () => ({ innerHTML: '', value: '', hidden: false, checked: true, addEventListener() {} });
global.document = { getElementById: id => id in blocks ? { textContent: blocks[id] } : (els[id] ??= el()),
                    querySelector: s => (els[s] ??= el()) };
global.matchMedia = () => ({ matches: false });
eval(page.match(/<script>([\s\S]*?)<\/script>/)[1]);
console.log(JSON.stringify({ runs: els['#runs'].innerHTML, cmp: els['#cmp'].innerHTML, plots: els['#plots'].hidden,
                             traj: els['#traj'].innerHTML, sc: els['#sc'].innerHTML }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_compare_script_runs_without_plotly(tmp_path):
    rows, params, metrics = _compare_input()
    page = tmp_path / "compare.html"
    page.write_text(board.compare_html(rows, params, metrics, None))
    out = subprocess.run(["node", "-e", _NODE_STUB, page.as_posix()], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["plots"] is True
    assert "<th>DW</th>" in got["runs"] and "a&lt;/script&gt;b" in got["runs"] and RUN["fail"] in got["runs"]
    assert got["cmp"].startswith("<tr><th>metric</th><th>c</th><th>b_nodw</th><th>a</th></tr>")
    assert ('<td>area.cell</td><td>1031</td><td>1032 <small class="up">+0.1%</small></td>'
            '<td>1030 <small class="dn">-0.1%</small></td>') in got["cmp"]
    assert "<td>power.A[k_small]</td><td>0.2000</td><td>0.3000 <small class=\"up\">+50.0%</small>" in got["cmp"]
    assert got["traj"] == "<option>area.cell</option>"
    assert got["sc"].startswith("<option selected>label</option>")


def test_rows_from_ledger(tmp_path):
    with Ledger(tmp_path / "edr.db") as led:
        for r in _rows():
            led.upsert_run(r)
        raw = [dict(r) for r in led.db.execute("SELECT * FROM runs")]
    assert all(isinstance(r["counts"], str) for r in raw)
    text = board.narrow(raw, now=NOW)
    assert all(len(line) <= 48 for line in text.splitlines())
    assert text.splitlines()[2].startswith(" #1 DEAD")
    assert RUN["dead"] in board.status_html(raw, [], {"local": {"cores": 4}}, now=NOW)

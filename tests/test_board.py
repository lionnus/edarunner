"""Tests of edarunner.board on rows shaped like the database `runs` table."""

import json
import re
import shutil
import subprocess
import time

import pytest
from helpers_board import NOW, RUN, SOURCE, board_row, board_rows

from edarunner import board
from edarunner.db import Database


def test_order_and_state():
    ordered = [r["run_id"] for r in board.order(board_rows())]
    assert ordered[:3] == [RUN["dead"], RUN["stale"], RUN["run1"]]
    assert ordered[3] == RUN["run2"]
    assert ordered[4:] == [RUN["fail"], RUN["done"]]
    assert board.state_of(board_row("fail", "b", "INCOMPLETE:1f0s", "running")) == "incomplete"
    assert board.state_of(board_row("run1", "c", None, None)) == "running"
    assert board.is_live(board_row("done", "a", "KILLED:SIGTERM")) is False


@pytest.mark.parametrize("width", [48, 40])
def test_narrow_width_and_order(width):
    text = board.narrow(board_rows(), width=width, now=NOW)
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
        assert body[7] == "    10_long_name   0m ago synth/3    0f/1d"
    assert board.narrow([], now=NOW).splitlines()[-1] == "nothing live"
    assert board.narrow([board_row("done", "a", "done")], now=NOW).endswith("nothing live")


def test_wide_all_states():
    text = board.plain(board.wide(board_rows(), now=NOW))
    lines = text.splitlines()
    assert lines[0].split() == ["#", "label", "source", "host", "state", "phase", "stage/step", "age", "fail/done", "core-h"]
    assert len(lines) == 8 and "\x1b" not in text
    assert lines[2].split()[:3] == ["#1", "a", "gaaa111"] and " dead " in lines[2]
    assert lines[6].startswith("#5  b_nodw") and " incomplete " in lines[6] and " 1f/1d " in lines[6]
    assert lines[7].startswith("#6  a") and " done " in lines[7]
    assert board.wide([]) == "no runs"
    cut = board.plain(board.wide([board_row("done", "a", "ABANDONED:" + "x" * 60)], now=NOW))
    assert "ABANDONED:" + "x" * 30 in cut and "x" * 31 not in cut


def test_fail_done_adds_the_skipped_and_held_tasks():
    held = board_row("fail", "b", "INCOMPLETE:2f1s3h", counts={"done": 4, "failed": 2, "skipped": 1, "held": 3})
    plain = board_row("done", "a", "done", counts={"done": 4, "failed": 0, "skipped": 0, "held": 0})
    lines = board.plain(board.wide([held, plain], now=NOW)).splitlines()
    assert " 2f/4d/1s/3h " in lines[2] and " 0f/4d " in lines[3]


def test_a_stage_with_steps_is_starting_before_its_first_step():
    fresh = [board_row("run1", "c", "stage:pnr", "running", stage="pnr", step=None),
             board_row("fail", "b", "INCOMPLETE:1f0s", "running", stage="pnr", step=None)]
    cells = [ln.split()[6:8] for ln in board.plain(board.wide(fresh, now=NOW, totals={"pnr": 13})).splitlines()[2:]]
    assert cells[0] == ["pnr,", "starting"] and cells[1][0] == "pnr"
    assert "pnr, start" in board.narrow(fresh, now=NOW, totals={"pnr": 13})
    assert "starting" not in board.plain(board.wide(fresh, now=NOW))


def test_narrow_text_colours_the_state():
    text = board.narrow_text(board_rows(), now=NOW)
    assert text.plain == board.narrow(board_rows(), now=NOW)
    styled = {text.plain[s.start:s.end]: str(s.style) for s in text.spans}
    assert styled == {"DEAD": "red", "stale": "yellow", "RUN": "green"}


def test_plain_and_console_have_no_escape_codes(monkeypatch, capsys):
    table = board.wide(board_rows(), now=NOW)
    assert "\x1b" not in board.plain(table)
    monkeypatch.setenv("NO_COLOR", "1")
    board.console().print(table)
    assert "\x1b" not in capsys.readouterr().out


def test_cost():
    live = board_row("run1", "c", "stage:synth", "running", cores=4)
    assert board.cost(live, now=NOW) == pytest.approx(2.0 * 4)
    ended = board_row("done", "a", "done", age=3600)
    assert board.cost(ended) == pytest.approx(1.0)
    assert board.cost({"run_id": "x", "phase": "done"}) == 0.0


def test_a_number_prints_six_significant_digits_without_an_exponent() -> None:
    assert [board.num(v) for v in (123.4560000000001, 48213000000.0, 0.30000000000000004, -0.061, 452817.3, 2345678.9)] == [
        "123.456", "48213000000", "0.3", "-0.061", "452817", "2345679"]
    assert [board.num(v) for v in (1.5e-7, 1e-5, -0.0, 0, 3)] == ["0.00000015", "0.00001", "0", "0", "3"]
    assert board.num(None) is None


def test_run_detail():
    row = board_row("run1", "c", "stage:synth", "running", killed_by=None)
    stages = [{"stage": "synth", "task": "", "attempt": 1, "status": "running", "exit": None, "started": NOW - 7200,
               "ended": None, "signature": None},
              {"stage": "power", "task": "k_bad", "attempt": 1, "status": "failed", "exit": 1, "started": NOW - 60,
               "ended": NOW - 50, "signature": "boom: kernel bad failed"}]
    metrics = [{"stage": "synth", "step": 3, "task": "", "name": "area_cell_um2", "canonical": "design__instance__area",
                "value": 1031.5, "unit": "um2"}]
    text = board.plain(board.run_detail(row, stages, metrics, "line one\nline two\n", now=NOW))
    assert text.startswith(RUN["run1"] + "\nrunning  stage:synth  synth/3\n")
    assert "cost 2.0 core-h" in text and "tasks done 1 failed 0" in text and "command exit" in text
    assert "k_bad" in text and "boom: kernel bad failed" in text
    assert "area_cell_um2" in text and "design__instance__area" not in text and "1031.5" in text
    assert text.endswith("log tail\nline one\nline two")


def test_run_detail_labels_the_driver_exit_and_shows_task_counts_only_for_a_task_group():
    row = board_row("done", "a", "FAILED:synth", exit=5)
    stages = [{"stage": "synth", "task": "", "attempt": 1, "status": "failed", "exit": 2, "started": NOW - 600,
               "ended": NOW - 60, "signature": None}]
    text = board.plain(board.run_detail(row, stages, [], "", now=NOW))
    assert text.splitlines()[1].endswith("driver exit 5 (FAILED:synth)")
    assert "tasks " not in text and "command exit" in text and " 9m " in text


def test_status_html():
    rows = board_rows()
    events = [{"id": i, "ts": NOW - i, "actor": "watch", "run_id": RUN["dead"], "kind": "dead", "text": f"event {i}"}
              for i in range(60)]
    hosts = {"local": {"free_cores": 3, "free_ram_gb": 6.5}, "hostB": {"free_cores": 100, "free_gb": 700}}
    page = board.status_html(rows, events, hosts, now=NOW)
    assert 'name="viewport"' in page and "prefers-color-scheme:dark" in page
    for run_id in RUN.values():
        assert run_id in page
    assert page.index(RUN["dead"]) < page.index(RUN["stale"]) < page.index(RUN["run1"]) < page.index(RUN["done"])
    assert "event 59" in page and "event 10" in page and "event 9" not in page
    assert "<th>free_gb</th>" in page and "<td>hostB</td>" in page and "<td>6.5</td>" in page
    assert "&lt;" not in page.split("<body>")[0]
    assert board.status_html([], [], {}, now=NOW).count("<table>") == 3


def _json_blocks(page):
    return {m.group(1): json.loads(m.group(2)) for m in
            re.finditer(r'<script type="application/json" id="([^"]+)">(.*?)</script>', page, re.S)}


def _compare_input():
    rows = board_rows()[:3]
    rows[0]["retired"] = 1
    parameters = [{"run_id": r["run_id"], "key": k, "value": v, "source": "config"}
              for r in rows for k, v in (("DW", "0"), ("LANES", "8"), ("note", "a</script>b"))]
    metrics = []
    for i, r in enumerate(rows):
        metrics += [{"run_id": r["run_id"], "stage": "synth", "step": s, "task": "", "name": "area_cell_um2",
                     "canonical": "design__instance__area", "value": 1000 + 10 * s + i, "unit": "um2"} for s in (1, 2, 3)]
        metrics += [{"run_id": r["run_id"], "stage": "power", "step": None, "task": "k_small", "name": f"power_{ph}",
                     "canonical": f"power__{ph}", "value": 0.1 * (i + 1), "unit": "W"} for ph in ("A", "B", "total")]
        metrics += [{"run_id": r["run_id"], "stage": "power", "step": None, "task": "k_small", "name": "window_fs",
                     "canonical": "", "value": 48213000000.0 + i, "unit": "fs"}]
        # A metric with `record`: the step of record is 2, not the last step, and the third run has none.
        metrics += [{"run_id": r["run_id"], "stage": "synth", "step": s, "task": "", "name": "wns_ns", "canonical": "",
                     "value": 0.05 + 0.01 * i if s == 2 else 0.04, "unit": "ns", "record": int(s == 2 and i != 2)}
                    for s in (2, 3)]
        # A slack whose sign differs between the second and the third run.
        metrics += [{"run_id": r["run_id"], "stage": "synth", "step": None, "task": "", "name": "slack_ns", "canonical": "",
                     "value": (0.03, -0.01, 0.02)[i], "unit": "ns"}]
    return rows, parameters, metrics


def test_compare_html_without_plotly():
    rows, parameters, metrics = _compare_input()
    page = board.compare_html(rows, parameters, metrics, None)
    assert "<script src=" not in page
    blocks = _json_blocks(page)
    assert set(blocks) == {"edr-runs", "edr-parameters", "edr-metrics", "edr-areas", "edr-steps", "edr-tick", "edr-better"}
    assert [r["run_id"] for r in blocks["edr-runs"]] == [RUN["run1"], RUN["fail"], RUN["done"]]
    assert blocks["edr-runs"][1]["state"] == "incomplete" and "core-h" in blocks["edr-runs"][0]
    assert blocks["edr-parameters"] == parameters and blocks["edr-metrics"] == metrics
    assert blocks["edr-tick"] == {"runs": [RUN["done"]], "named": False} and blocks["edr-better"] == {}
    assert "</script>b" not in page.split("<h3>")[0].split("edr-parameters")[1]
    with_plotly = board.compare_html(rows, parameters, metrics, "plotly-2.35.2.min.js")
    assert '<script src="plotly-2.35.2.min.js"></script>' in with_plotly


def test_compare_html_ticks_two_done_runs_of_the_newest_source_one_per_label():
    # The run ids sort against the start times, so only a start time says which run is the newest.
    runs = [board_row("done", label, phase, run_id=run_id, source=source, started=NOW - age)
            for run_id, label, source, phase, age in (("r1", "a", "gnew", "done", 100), ("r2", "a", "gnew", "done", 200),
                                                      ("r3", "b", "gnew", "done", 300), ("r4", "c", "gold", "done", 900),
                                                      ("r5", "d", "gnew", "FAILED:pnr", 50))]
    blocks = _json_blocks(board.compare_html(runs, [], [], None))
    assert blocks["edr-tick"] == {"runs": ["r3", "r1"], "named": False}
    # label@source in the URL hash names the run that db.pick takes, the newest run that ended done.
    assert {r["run_id"]: r["pick"] for r in blocks["edr-runs"]} == {"r1": 1, "r2": 0, "r3": 1, "r4": 1, "r5": 1}
    named = _json_blocks(board.compare_html(runs, [], [], None, tick=["r4", "r2"], better={"area": "lower"}))
    assert named["edr-tick"] == {"runs": ["r4", "r2"], "named": True} and named["edr-better"] == {"area": "lower"}


_NODE_STUB = r"""
const fs = require('fs'); const page = fs.readFileSync(process.argv[1], 'utf8');
const blocks = {}; for (const m of page.matchAll(/<script type="application\/json" id="([^"]+)">([\s\S]*?)<\/script>/g)) blocks[m[1]] = m[2];
const els = {}; const el = () => ({ innerHTML: '', value: '', hidden: false, checked: true, addEventListener() {} });
global.document = { getElementById: id => id in blocks ? { textContent: blocks[id] } : (els[id] ??= el()),
                    querySelector: s => (els[s] ??= el()) };
global.matchMedia = () => ({ matches: false });
global.location = { hash: process.argv[3] || '' };
const PLOTS = {}; if (process.argv[2]) global.Plotly = { react: (id, data) => { PLOTS[id] = data; } };
eval(page.match(/<script>([\s\S]*?)<\/script>/)[1]);
console.log(JSON.stringify({ runs: els['#runs'].innerHTML, cmp: els['#cmp'].innerHTML, plots: els['#plots'].hidden,
                             traj: els['#traj'].innerHTML, sc: els['#sc'].innerHTML, area: els['#areat'].innerHTML,
                             areas: els['#areas'].textContent, ad: els['#ad'].innerHTML, pm: els['#pm'].innerHTML,
                             parts: (PLOTS.phasep || []).map(t => t.name), folded: !els['#rd'].open,
                             lost: els['#lost'].textContent }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_compare_script_runs_without_plotly(tmp_path):
    rows, parameters, metrics = _compare_input()
    areas = {r["run_id"]: {"stage": "synth", "step": 3, "source_file": f"reports/3/{i}.rpt",
                           "rows": [["<top>", 0, 100.0 + i], ["i_top", 1, 90.0 + i], ["i_top/x", 2, 50.0 * (i + 1)]]}
             for i, r in enumerate(rows[:2])}
    page = tmp_path / "compare.html"
    page.write_text(board.compare_html(rows, parameters, metrics, None, areas, {3: "synth"}, [RUN["run1"], RUN["fail"]],
                                       {"area_cell_um2": "lower", "slack_ns": "higher", "window_fs": "higher"}))

    def run(plotly: str = "", hash_: str = "") -> dict:
        out = subprocess.run(["node", "-e", _NODE_STUB, page.as_posix(), plotly, hash_], capture_output=True, text=True,
                             timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    got = run()
    assert got["plots"] is True
    assert "<th>DW</th>" in got["runs"] and "a&lt;/script&gt;b" in got["runs"] and RUN["fail"] in got["runs"]
    # One filter box per column, the named runs ticked, the runs table folded, and the retired run marked.
    assert got["runs"].count('<input class="f"') == 10 and got["runs"].count(" checked>") == 2 and got["folded"]
    assert f'<span title="{RUN["done"]}">a</span> <small>retired</small>' in got["runs"]
    # A name may break after an underscore, and the header of a run is label@source.
    assert "<td>area_<wbr>cell_<wbr>um2 <small>" in got["cmp"] and "<th>b_<wbr>nodw<wbr><small>@gaaa111</small></th>" in got["cmp"]
    cmp = got["cmp"].replace("<wbr>", "")
    assert cmp.startswith("<tr><th>metric</th><th>c<small>@gaaa111</small></th><th>b_nodw<small>@gaaa111</small></th></tr>")
    # The rows go by task, the metrics without a task first, each with its unit.
    assert re.findall(r'<tr(?: class="task")?><td[^>]*>(.*?)</td>', cmp) == [
        "flow", "area_cell_um2 <small>um2</small>", "slack_ns <small>ns</small>", "wns_ns <small>ns</small>", "k_small",
        "power_A <small>W</small>", "power_B <small>W</small>", "power_total <small>W</small>", "window_fs <small>fs</small>"]
    # A value prints as board.num prints it, and only a metric with `better` gets a colour: red for worse, green for
    # better. Across a sign change, the difference takes the place of the percent.
    assert ('<td>area_cell_um2 <small>um2</small></td><td>1031 <small>synth 3</small></td>'
            '<td>1032 <small>synth 3</small> <small class="worse">+0.1%</small></td>') in cmp
    assert '<td>slack_ns <small>ns</small></td><td>-0.01</td><td>0.02 <small class="better">+0.03</small></td>' in cmp
    assert '<td>power_A <small>W</small></td><td>0.2</td><td>0.3 <small>+50.0%</small></td>' in cmp
    assert ('<td>window_fs <small>fs</small></td><td>48213000001</td>'
            '<td>48213000002 <small class="better">+0.0%</small></td>') in cmp
    assert '<td>wns_ns <small>ns</small></td><td>0.06 <small>synth 2</small></td><td> <small>missing</small></td>' in cmp
    assert "design__instance__area" not in cmp
    assert got["traj"] == '<option value="area_cell_um2">area_cell_um2</option><option value="wns_ns">wns_ns</option>'
    assert got["sc"].startswith('<option value="label" selected>label</option>')
    assert got["pm"].startswith('<option value="area_cell_um2" selected>')
    assert got["ad"] == '<option value="1" selected>1</option><option value="2">2</option>'
    assert "<tr><td>i_top</td><td>91</td><td>90</td><td>-1</td><td>-1.1%</td></tr>" in got["area"]
    assert "<tr><td>&lt;top&gt;</td><td>101</td><td>100</td><td>-1</td><td>-1.0%</td></tr>" in got["area"]
    # The area delta opens on the ticked run with an area report, then on the next run with one.
    assert got["areas"] == "A: synth step 3, reports/3/1.rpt; B: synth step 3, reports/3/0.rpt"
    # With Plotly, the power parts are the metrics whose canonical name is power__* without power__total.
    assert run("plotly")["parts"] == ["power_A", "power_B"]
    # The URL hash ticks its runs in its order and names a handle that no run has.
    got = run("", "#runs=b_nodw@gaaa111,a@gaaa111,zz@x")
    assert got["cmp"].replace("<wbr>", "").startswith(
        "<tr><th>metric</th><th>b_nodw<small>@gaaa111</small></th><th>a<small>@gaaa111</small></th></tr>")
    assert got["lost"] == "no run is named zz@x" and got["folded"] and got["runs"].count(" checked>") == 2
    # Without named runs, the runs table is open on the default ticks.
    page.write_text(board.compare_html(rows, parameters, metrics, None, areas))
    got = run()
    assert not got["folded"] and got["runs"].count(" checked>") == 1 and got["lost"] == ""


def test_rows_from_db(tmp_path):
    with Database(tmp_path / "edr.db") as db:
        for r in board_rows():
            db.upsert_run(r)
        raw = [dict(r) for r in db.conn.execute("SELECT * FROM runs")]
    assert all(isinstance(r["counts"], str) for r in raw)
    text = board.narrow(raw, now=NOW)
    assert all(len(line) <= 48 for line in text.splitlines())
    assert text.splitlines()[2].startswith(" #1 DEAD")
    assert RUN["dead"] in board.status_html(raw, [], {"local": {"cores": 4}}, now=NOW)


def test_the_proposed_retire_names_the_phase_or_asks_for_a_reason() -> None:
    row = {"label": "a", "batch": "demo", "phase": "KILLED:SIGTERM", "root": "/tmp/edr/demo/a"}
    assert board.triage_cmd(row, "killed", {}) == "edr retire a@demo --why KILLED:SIGTERM"
    assert board.triage_cmd({**row, "phase": None}, "imported", {}) == "edr retire a@demo --why '<why>'"
    assert board.triage_cmd({**row, "root": None}, "killed", {}) is None  # no tree: a retire would only mark the row


def test_a_label_at_a_batch_with_two_runs_prints_a_run_id_prefix() -> None:
    failed = board_row("fail", "a", "FAILED:pnr")
    dirty = board_row("fail", "a", "done", run_id=RUN["fail"] + "-dirty-0badc0de", source=SOURCE + "-dirty-0badc0de")
    other = board_row("done", "b", "done")
    runs = [failed, dirty, other]
    # The clean id starts the dirty one, so only the whole clean id names the clean run alone.
    assert board.handles(runs) == {failed["run_id"]: RUN["fail"], dirty["run_id"]: RUN["fail"] + "-", other["run_id"]: "b@demo"}
    assert board.handle(dirty, runs) == RUN["fail"] + "-" and board.handle(dirty) == "a@demo"
    assert board.triage_cmd(failed, "failed", {}, runs) == f"edr retire {RUN['fail']} --why FAILED:pnr"
    assert board.prefix(RUN["done"], list(RUN.values())) == "20261001_0900_a"

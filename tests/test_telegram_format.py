"""The Telegram HTML of edarunner.notify.telegram.format on fixed rows."""

import time

from edarunner.notify.telegram import format as fmt
from test_board import NOW, RUN, _row, _rows


def test_board_is_one_line_per_run_then_the_counts():
    lines = fmt.board(_rows(), now=NOW, totals={"synth": 4}).splitlines()
    assert lines[:3] == ["🔴 <code>a@demo</code> dead · synth 3/4 · 1h", "🟡 <code>b_nodw@demo</code> stale · synth 3/4 · 15m",
                         "🟢 <code>c@demo</code> synth 3/4 · 0m"]
    assert lines[4:] == ["🟠 <code>b_nodw@demo</code> incomplete · 1h", "⚪ <code>a@demo</code> done · 1h",
                         "<i>1 dead · 1 incomplete · 1 stale · 2 running · 1 done</i>"]
    done = [_row("done", "<a>", "done")]
    assert fmt.board(done * 32, now=NOW).splitlines()[-3:] == [
        "<i>… and 2 more</i>", "<i>nothing live</i>", "<i>32 done</i>"]
    assert "&lt;a&gt;@demo" in fmt.board(done, now=NOW) and fmt.board([], now=NOW) == "<i>no runs</i>"


def test_alert_marks_the_state_and_escapes_the_reason():
    assert fmt.alert("demo", "hung a@demo\nno progress <3 h", "edr stop a@demo --why hung") == (
        "🔴 <b>demo · hung</b> <code>a@demo</code>\nno progress &lt;3 h\n<code>edr stop a@demo --why hung</code>")
    assert fmt.alert("demo", "watch stale\nno watch.json") == "<b>demo · watch stale</b>\nno watch.json"


def test_run_detail_of_a_dead_run():
    row = _row("dead", "a", "stage:synth", "dead", age=5000)
    hb = {"step": 3, "step_name": "elaborate", "stage": "synth", "last_log": "step 3 <elaborate>\n\n"}
    assert fmt.run_detail(row, hb, NOW).splitlines() == [
        "🔴 <code>a@demo</code> dead", "stage synth, step 3 elaborate", "on local, 1h",
        "<code>edr run a@demo --stage synth --from elaborate</code>", "<pre>step 3 &lt;elaborate&gt;</pre>"]


def test_events_newest_first_with_handles():
    rows = [{"ts": NOW - 60, "kind": "launch", "run_id": RUN["dead"], "text": f"{RUN['dead']} on local"},
            {"ts": NOW, "kind": "keep", "run_id": RUN["dead"], "text": "keep"}]
    hm = [time.strftime("%H:%M", time.localtime(t)) for t in (NOW, NOW - 60)]
    assert fmt.events(rows, {RUN["dead"]: "a@demo"}).splitlines() == [
        f"{hm[0]} <b>keep</b> <code>a@demo</code>", f"{hm[1]} <b>launch</b> <code>a@demo</code>", "    <i>a@demo on local</i>"]
    assert fmt.events([], {}) == "<i>no events</i>"


def test_hosts_and_licences():
    probe = {"host": "hostA", "cores": 32, "load": 20.6, "free_ram_gb": 283.0, "total_ram_gb": 376.0,
             "free_gb": 1343.0, "total_gb": 1538.0, "gpus": 1, "gpus_idle": 1}
    probe["marks"] = {"cores": "🟡", "ram": "🟢", "scratch": "🟢", "gpu": "🟢"}
    no_gpu = {**probe, "host": "hostC", "gpus": 0, "gpus_idle": 0}
    assert fmt.hosts([{"host": "hostB", "error": "timeout"}, probe, no_gpu]).splitlines() == [
        "⚫ <b>hostB</b> · <i>no answer</i>",
        "<b>hostA</b> · 🟡 cores 21/32 · 🟢 ram 93/376 GB · 🟢 scratch 195/1538 GB · 🟢 gpu 0/1",
        "<b>hostC</b> · 🟡 cores 21/32 · 🟢 ram 93/376 GB · 🟢 scratch 195/1538 GB"]
    assert fmt.licences([{"licence": "demo", "used": 5, "pool": 8}, {"licence": "x", "note": "unknown: <none>"}]) == (
        "<b>demo</b> · 5/8 seats used\n<b>x</b> · <i>unknown: &lt;none&gt;</i>")


def test_run_detail_strips_colour_codes_from_the_log_line():
    row = _row("run1", "c", "stage:synth", "running")
    hb = {"last_log": "ok\n\x1b[1;31mError:\x1b[0m timing <met>\x1b[K\n"}
    assert fmt.run_detail(row, hb, NOW).splitlines()[-1] == "<pre>Error: timing &lt;met&gt;</pre>"

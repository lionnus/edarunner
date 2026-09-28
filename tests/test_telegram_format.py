"""The Telegram HTML of edarunner.notify.telegram.format on fixed rows."""

import time

from helpers_board import NOW, RUN, board_row, board_rows

from edarunner.notify.telegram import format as fmt


def test_board_has_running_and_finished_sections_then_the_counts_and_a_legend():
    rows = [*board_rows(), board_row("done", "old", "FAILED:synth", age=200_000),
            board_row("run1", "fresh", "stage:synth", "running", step=None)]
    assert fmt.board(rows, now=NOW, totals={"synth": 4}, names={3: "elaborate"}).splitlines() == [
        "<b>Running</b>",
        "🔴 <code>a@demo</code> dead, synth 3/4 elaborate, 2h",
        "🟡 <code>b_nodw@demo</code> stale, synth 3/4 elaborate, 2h",
        "🟢 <code>c@demo</code> synth 3/4 elaborate, 2h",
        "🟢 <code>dddddddddddddddddddddddddddddddddddddddd@sweep10_long_name</code> synth 3/4 elaborate, 2h",
        "🟢 <code>fresh@demo</code> synth, starting, 2h",
        "",
        "<b>Finished in the last 24 hours</b>",
        "🟠 <code>b_nodw@demo</code> incomplete, ended 1h ago",
        "⚪ <code>a@demo</code> done, ended 1h ago",
        "<i>and 1 older run: /status all</i>",
        "",
        "<i>1 dead, 1 failed, 1 incomplete, 1 stale, 3 running, 1 done</i>",
        "<i>🔴 dead (driver gone), 🟡 stale (no heartbeat for a while), 🟢 running, 🟠 incomplete (some tasks failed), "
        "⚪ done</i>"]
    every = fmt.board(rows, now=NOW, everything=True).splitlines()
    assert every[7:12] == ["<b>Finished</b>", "🔴 <code>old@demo</code> failed, ended "
                           + time.strftime("%d.%m %H:%M", time.localtime(NOW - 200_000)),
                           "🟠 <code>b_nodw@demo</code> incomplete, ended 1h ago", "⚪ <code>a@demo</code> done, ended 1h ago", ""]
    assert every[-1] == "<i>🔴 dead (driver gone) or failed, 🟡 stale (no heartbeat for a while), 🟢 running, " \
                        "🟠 incomplete (some tasks failed), ⚪ done</i>"
    done = [board_row("done", "<a>", "done")]
    assert fmt.board(done * 32, now=NOW).splitlines()[:4] == ["<b>Running</b>", "<i>nothing live</i>", "",
                                                              "<b>Finished in the last 24 hours</b>"]
    assert "<i>… and 2 more</i>" in fmt.board(done * 32, now=NOW).splitlines()
    assert "&lt;a&gt;@demo" in fmt.board(done, now=NOW) and fmt.board([], now=NOW) == "<i>no runs</i>"


def test_run_detail_of_a_dead_run():
    row = board_row("dead", "a", "stage:synth", "dead", age=5000)
    hb = {"step": 3, "step_name": "elaborate", "stage": "synth", "last_log": "step 3 <elaborate>\n\n"}
    assert fmt.run_detail(row, hb, NOW).splitlines() == [
        "🔴 <code>a@demo</code> dead", "stage synth, step 3 elaborate", "on local, 1h",
        "<code>edr continue a@demo --stage synth --from elaborate</code>", "<pre>step 3 &lt;elaborate&gt;</pre>"]


def test_events_newest_first_with_handles():
    rows = [{"ts": NOW - 60, "kind": "launch", "run_id": RUN["dead"], "text": f"{RUN['dead']} on local"},
            {"ts": NOW, "kind": "keep", "run_id": RUN["dead"], "text": "keep"}]
    hm = [time.strftime("%H:%M", time.localtime(t)) for t in (NOW, NOW - 60)]
    assert fmt.events(rows, {RUN["dead"]: "a@demo"}).splitlines() == [
        f"{hm[0]} <b>keep</b> <code>a@demo</code>", f"{hm[1]} <b>launch</b> <code>a@demo</code>", "    <i>a@demo on local</i>"]
    assert fmt.events([], {}) == "<i>no events</i>"


def test_hosts_and_tools():
    row = {"host": "hostA", "cores": 32, "free_cores": 11.4, "free_ram_gb": 283.0, "total_ram_gb": 376.0,
           "free_gb": 1343.0, "total_gb": 1538.0, "gpus": 1, "gpus_idle": 1, "runs": {"p2": 1, "p1": 2},
           "our_cores": 12.5, "our_gb": 40.0, "start": True, "why": "", "note": ""}
    full = {**row, "host": "hostC", "gpus": 0, "gpus_idle": 0, "runs": {}, "start": False,
            "why": "13 GB scratch free, under the floor of 100 GB", "note": "your runs use 12 of its 20 busy cores"}
    assert fmt.hosts([row, full, {"host": "hostB", "error": "timeout", "start": None}]).splitlines() == [
        "🟢 <b>hostA</b> free 11.4/32 cores, 283/376 GB RAM, 1343/1538 GB scratch, 1/1 GPUs; yours: p1 2, p2 1, 12.5 cores, "
        "40 GB scratch",
        "🔴 <b>hostC</b> free 11.4/32 cores, 283/376 GB RAM, 1343/1538 GB scratch; none of yours",
        "    <i>13 GB scratch free, under the floor of 100 GB</i>",
        "    <i>your runs use 12 of its 20 busy cores</i>",
        "⚫ <b>hostB</b> <i>no answer</i>",
        "",
        "<i>🟢 a run can start, 🔴 no run can start, ⚫ no answer</i>"]
    assert fmt.hosts([]) == "<i>no hosts</i>"
    rows = [{"tool": "fc", "hosts": {"hostA": "", "hostB": "2024.09"}, "free": 5, "total": 8},
            {"tool": "vcs", "hosts": {"hostA": ""}, "free": 3}, {"tool": "gpu", "hosts": {}, "total": 2},
            {"tool": "x", "hosts": {}, "note": "unknown: <none>"}, {"tool": "sh", "hosts": {}}]
    assert fmt.tools(rows).splitlines() == ["<b>fc</b> 3/8 seats used, hostA, hostB", "<b>vcs</b> 3 seats left, hostA",
                                            "<b>gpu</b> 2 seats", "<b>x</b> <i>unknown: &lt;none&gt;</i>", "<b>sh</b>"]


def test_run_detail_strips_colour_codes_from_the_log_line():
    row = board_row("run1", "c", "stage:synth", "running")
    hb = {"last_log": "ok\n\x1b[1;31mError:\x1b[0m timing <met>\x1b[K\n"}
    assert fmt.run_detail(row, hb, NOW).splitlines()[-2:] == ["<pre>ok", "Error: timing &lt;met&gt;</pre>"]


def test_a_compare_of_300_rows_keeps_its_first_rows_under_the_message_limit():
    rows = [f"area_{n} um2\n  alpha  {n}.5 (pnr 12)\n  beta   {n}.7 (pnr 12)  +0.1%" for n in range(300)]
    missing = ["missing: gamma has pnr steps 8 to 9"]
    text = fmt.first_rows(rows, missing)
    kept = text.count(" um2\n")
    assert len("demo: compare\n" + text) < 4096 and text.startswith("\n".join(rows[:2]) + "\n")
    assert text.splitlines()[-2:] == [f"… {300 - kept} more rows", *missing] and kept > 40
    assert fmt.first_rows(rows[:3], missing) == "\n".join([*rows[:3], *missing])

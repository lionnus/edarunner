"""The text of the orphan, dead, hung, over_budget, stopped, held, done and metrics alerts, exactly as each channel sends
it."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest import mock

import pytest
from helpers_telegram import FakeApi, FakeDatabase, make_project, make_site, make_user

from edarunner.config import load_project
from edarunner.model import Mail, Ntfy
from edarunner.notify import alerts
from edarunner.notify.mail import MailNotifier
from edarunner.notify.ntfy import NtfyNotifier
from edarunner.notify.telegram import TelegramBot
from helpers_driver import DEMO

NOW = 1_800_000_000.0
PROJECT = load_project(DEMO)
PROJECT = replace(PROJECT, limits=replace(PROJECT.limits, grace_s=3600, kill_hung=True))
RUN = {"run_id": "20260926_1200_b_demo_gabc1234", "label": "b", "batch": "demo", "host": "hostA", "stage": "synth",
       "step": 3, "phase": "stage:synth", "handle": "slurm:4711"}
HB = {"host": "hostA", "stage": "synth", "step": 3, "step_name": "elaborate", "driver_pid": 4711, "updated": NOW - 3000,
      "phase": "stage:synth", "last_log": "Information: elaborating top\n", "over_budget": "synth"}
ENDED = {**RUN, "phase": "OVER_BUDGET:synth", "root": "/scratch/user/edr/demo/b"}
ORPHAN = {"key": "orphan:hostA:4711", "host": "hostA", "pid": 4711, "label": "fc_shell", "etimes": 12000,
          "cwd": "/home/me/work", "phase": "fc_shell -f /home/me/work/run.tcl -x " + "y" * 120}
ALERTS = {
    "orphan": alerts.orphan_alert(None, ORPHAN),
    "orphan of a dead run": alerts.orphan_alert(PROJECT, {**ORPHAN, "owner": RUN["run_id"], "owner_handle": "demo/b@demo",
                                                          "owner_state": "dead"}),
    "dead": alerts.run_alert(PROJECT, RUN, "dead", ["heartbeat older than 2700 s, driver 4711 gone on hostA"], HB, NOW),
    "hung": alerts.run_alert(PROJECT, RUN, "hung", ["hung: no progress since 14.01 03:00"], HB, NOW),
    "over_budget": alerts.run_alert(PROJECT, RUN, "over_budget", ["over_budget: stage synth"], HB, NOW),
    "over_budget, ended": alerts.run_alert(PROJECT, ENDED, "over_budget", [], {**HB, "phase": "OVER_BUDGET:synth", "exit": 9},
                                           NOW, ["pnr", "export"]),
    "stopped": alerts.run_alert(PROJECT, {**ENDED, "phase": "STOPPED"}, "stopped", [], {**HB, "phase": "STOPPED", "exit": 10},
                                NOW, ["pnr"]),
    "held": alerts.run_alert(PROJECT, RUN, "held", ["held by the scheduler"], {}, NOW),
    "done": alerts.run_alert(PROJECT, {**ENDED, "phase": "done"}, "done", [],
                             {**HB, "phase": "done", "exit": 0, "elapsed_s": 5400}, NOW),
    "metrics": alerts.metrics_alert(RUN, "6 new: area_um2, wns_ns at pnr 8, 9"),
}


def telegram(a: alerts.Alert, tmp_path) -> str:
    bot = TelegramBot(make_user(tmp_path).telegram, make_site(), make_project(tmp_path), FakeDatabase(), None,
                      str(tmp_path / "telegram.token"))
    bot.api = FakeApi()
    bot.send(a)
    return bot.api.of("sendMessage")[0]["text"]


def ntfy(a: alerts.Alert, monkeypatch) -> str:
    bodies = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: bodies.append(json.loads(req.data))
                        or mock.MagicMock(status=200, __enter__=lambda self: self))
    NtfyNotifier(PROJECT, Ntfy(topic="t")).send(a)
    return bodies[0]["title"] + "\n" + bodies[0]["message"]


def mail(a: alerts.Alert) -> str:
    with mock.patch("smtplib.SMTP") as smtp:
        MailNotifier(PROJECT, Mail(host="h", sender="f@example.org", to=["t@example.org"])).send(a)
    [(msg,), _] = smtp.return_value.__enter__.return_value.send_message.call_args
    return msg["Subject"] + "\n" + msg.get_content()


# (Telegram HTML, mail subject and body) per kind; ntfy sends the mail text with the code lines not indented.
EXPECTED = {
    'orphan': (
        '🔴 <b>demo: tool process with no run on</b> <code>hostA</code>\n'
        'Your process fc_shell runs on hostA, and no edarunner run owns it. It may hold a licence seat.\n'
        '\n'
        'process: fc_shell, pid 4711\n'
        'running for: 3h\n'
        'directory: /home/me/work\n'
        '<code>fc_shell -f /home/me/work/run.tcl -x yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy…</code>\n'
        '\n'
        'Check it:\n'
        '<code>ssh hostA ps -o pid,etime,args -p 4711</code>\n'
        'If it is yours and stale, end it:\n'
        '<code>ssh hostA kill 4711</code>\n'
        'edarunner never kills a process that belongs to no project.',
        '🔴 demo: tool process with no run on hostA\n'
        'Your process fc_shell runs on hostA, and no edarunner run owns it. It may hold a licence seat.\n'
        '\n'
        'process: fc_shell, pid 4711\n'
        'running for: 3h\n'
        'directory: /home/me/work\n'
        '    fc_shell -f /home/me/work/run.tcl -x yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy…\n'
        '\n'
        'Check it:\n'
        '    ssh hostA ps -o pid,etime,args -p 4711\n'
        'If it is yours and stale, end it:\n'
        '    ssh hostA kill 4711\n'
        'edarunner never kills a process that belongs to no project.\n',
    ),
    'orphan of a dead run': (
        '🔴 <b>demo: tool process of an ended run on</b> <code>hostA</code>\n'
        'Your process fc_shell runs on hostA for the run demo/b@demo, whose driver is gone. It may hold a licence seat.\n'
        '\n'
        'process: fc_shell, pid 4711\n'
        'running for: 3h\n'
        'directory: /home/me/work\n'
        '<code>fc_shell -f /home/me/work/run.tcl -x yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy…</code>\n'
        '\n'
        'Check it:\n'
        '<code>ssh hostA ps -o pid,etime,args -p 4711</code>\n'
        'If it is yours and stale, end it:\n'
        '<code>ssh hostA kill 4711</code>\n'
        'edarunner never kills it, since kill_orphan is off.',
        '🔴 demo: tool process of an ended run on hostA\n'
        'Your process fc_shell runs on hostA for the run demo/b@demo, whose driver is gone. It may hold a licence seat.\n'
        '\n'
        'process: fc_shell, pid 4711\n'
        'running for: 3h\n'
        'directory: /home/me/work\n'
        '    fc_shell -f /home/me/work/run.tcl -x yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy…\n'
        '\n'
        'Check it:\n'
        '    ssh hostA ps -o pid,etime,args -p 4711\n'
        'If it is yours and stale, end it:\n'
        '    ssh hostA kill 4711\n'
        'edarunner never kills it, since kill_orphan is off.\n',
    ),
    'dead': (
        '🔴 <b>demo: driver gone for</b> <code>b@demo</code>\n'
        'The run has written no heartbeat for 50m, and its driver 4711 is gone from hostA. Nothing runs until it is resumed.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '<pre>Information: elaborating top</pre>\n'
        '\n'
        'Resume it from the last step:\n'
        '<code>edr continue b@demo --stage synth --from elaborate</code>\n'
        'edarunner resumes it once by itself when the stage has a resume command.',
        '🔴 demo: driver gone for b@demo\n'
        'The run has written no heartbeat for 50m, and its driver 4711 is gone from hostA. Nothing runs until it is resumed.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '    Information: elaborating top\n'
        '\n'
        'Resume it from the last step:\n'
        '    edr continue b@demo --stage synth --from elaborate\n'
        'edarunner resumes it once by itself when the stage has a resume command.\n',
    ),
    'hung': (
        '🔴 <b>demo: no progress in</b> <code>b@demo</code>\n'
        'The run is alive, but it has shown no progress since 14.01 03:00: the step, the log, the tree size and the CPU time stand still. The tool may wait for a licence, or it is stuck.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '<pre>Information: elaborating top</pre>\n'
        '\n'
        'If it is stuck, stop it:\n'
        '<code>edr stop b@demo --why hung</code>\n'
        'edarunner sends SIGTERM to its processes 1h after this alert unless it has a keep.\n'
        '+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.',
        '🔴 demo: no progress in b@demo\n'
        'The run is alive, but it has shown no progress since 14.01 03:00: the step, the log, the tree size and the CPU time stand still. The tool may wait for a licence, or it is stuck.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '    Information: elaborating top\n'
        '\n'
        'If it is stuck, stop it:\n'
        '    edr stop b@demo --why hung\n'
        'edarunner sends SIGTERM to its processes 1h after this alert unless it has a keep.\n'
        '+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.\n'
        '\n'
        'Stop: edr stop b@demo --after-task --why hung\n'
        '+6h: edr keep b@demo --hours 6\n'
        '+12h: edr keep b@demo --hours 12\n'
        '+24h: edr keep b@demo --hours 24\n',
    ),
    'over_budget': (
        '🔴 <b>demo: over budget in</b> <code>b@demo</code>\n'
        'Stage synth went past its budget of 1 h and 1 GB. The stage runs to its end; more time on the budget before then lets the run go on.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '<pre>Information: elaborating top</pre>\n'
        '\n'
        'Stop it now if the rest of the stage is of no use:\n'
        '<code>edr stop b@demo --why over-budget</code>\n'
        '+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.',
        '🔴 demo: over budget in b@demo\n'
        'Stage synth went past its budget of 1 h and 1 GB. The stage runs to its end; more time on the budget before then lets the run go on.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '    Information: elaborating top\n'
        '\n'
        'Stop it now if the rest of the stage is of no use:\n'
        '    edr stop b@demo --why over-budget\n'
        '+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.\n'
        '\n'
        'Stop: edr stop b@demo --after-task --why over_budget\n'
        '+6h: edr keep b@demo --hours 6\n'
        '+12h: edr keep b@demo --hours 12\n'
        '+24h: edr keep b@demo --hours 24\n',
    ),
    'over_budget, ended': (
        '🔴 <b>demo: over budget in</b> <code>b@demo</code>\n'
        'Stage synth went past its budget of 1 h and 1 GB. The run ended OVER_BUDGET. It did not run pnr and export.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '<pre>Information: elaborating top</pre>\n'
        '\n'
        'Run the stages left on the same tree:\n'
        '<code>edr continue b@demo</code>\n'
        'Continue runs pnr and export as one new run on hostA, in the tree /scratch/user/edr/demo/b. On Telegram it asks once more.',
        '🔴 demo: over budget in b@demo\n'
        'Stage synth went past its budget of 1 h and 1 GB. The run ended OVER_BUDGET. It did not run pnr and export.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '    Information: elaborating top\n'
        '\n'
        'Run the stages left on the same tree:\n'
        '    edr continue b@demo\n'
        'Continue runs pnr and export as one new run on hostA, in the tree /scratch/user/edr/demo/b. On Telegram it asks once more.\n'
        '\n'
        'Continue: edr continue b@demo\n',
    ),
    'stopped': (
        '⚫ <b>demo: stopped run</b> <code>b@demo</code>\n'
        'A stop ended the run in stage synth. It did not run pnr.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '<pre>Information: elaborating top</pre>\n'
        '\n'
        'Run the stages left on the same tree:\n'
        '<code>edr continue b@demo</code>\n'
        'Continue runs pnr as one new run on hostA, in the tree /scratch/user/edr/demo/b. On Telegram it asks once more.',
        '⚫ demo: stopped run b@demo\n'
        'A stop ended the run in stage synth. It did not run pnr.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        '    Information: elaborating top\n'
        '\n'
        'Run the stages left on the same tree:\n'
        '    edr continue b@demo\n'
        'Continue runs pnr as one new run on hostA, in the tree /scratch/user/edr/demo/b. On Telegram it asks once more.\n'
        '\n'
        'Continue: edr continue b@demo\n',
    ),
    'held': (
        '🟠 <b>demo: scheduler holds</b> <code>b@demo</code>\n'
        'The scheduler holds the job, and it starts only after someone releases it.\n'
        '\n'
        'job: slurm:4711\n'
        '\n'
        'Release it with the scheduler, or cancel it:\n'
        '<code>edr stop b@demo --why held</code>',
        '🟠 demo: scheduler holds b@demo\n'
        'The scheduler holds the job, and it starts only after someone releases it.\n'
        '\n'
        'job: slurm:4711\n'
        '\n'
        'Release it with the scheduler, or cancel it:\n'
        '    edr stop b@demo --why held\n',
    ),
    'done': (
        '⚪ <b>demo: run done</b> <code>b@demo</code>\n'
        'The run ended done after 1h.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        'See its numbers:\n'
        '<code>edr metrics --run b@demo</code>',
        '⚪ demo: run done b@demo\n'
        'The run ended done after 1h.\n'
        '\n'
        'stage: synth, step 3 elaborate\n'
        'host: hostA\n'
        '\n'
        'See its numbers:\n'
        '    edr metrics --run b@demo\n',
    ),
    'metrics': (
        '⚪ <b>demo: new metrics of</b> <code>b@demo</code>\n'
        '6 new: area_um2, wns_ns at pnr 8, 9.\n'
        '\n'
        'See them:\n'
        '<code>edr metrics --run b@demo</code>',
        '⚪ demo: new metrics of b@demo\n'
        '6 new: area_um2, wns_ns at pnr 8, 9.\n'
        '\n'
        'See them:\n'
        '    edr metrics --run b@demo\n',
    ),
}


@pytest.mark.parametrize("kind", ALERTS)
def test_every_channel_says_the_same(kind, tmp_path, monkeypatch):
    tg, text = EXPECTED[kind]
    assert telegram(ALERTS[kind], tmp_path) == tg
    assert mail(ALERTS[kind]) == text
    assert ntfy(ALERTS[kind], monkeypatch) == "\n".join(ln.removeprefix("    ") for ln in text.rstrip("\n").split("\n"))


def test_an_alert_stays_under_the_message_limit(tmp_path, monkeypatch):
    a = alerts.run_alert(PROJECT, RUN, "failed", ["the job left the scheduler: " + "x" * 9000], {}, NOW)
    assert len(telegram(a, tmp_path)) <= alerts.LIMIT + 2 and len(ntfy(a, monkeypatch)) <= alerts.LIMIT + 100


def test_the_incomplete_alert_counts_the_held_tasks():
    counts = {"done": 1, "failed": 2, "skipped": 0, "held": 3}
    a = alerts.run_alert(PROJECT, {**ENDED, "phase": "INCOMPLETE:2f0s3h"}, "incomplete", [],
                         {**HB, "phase": "INCOMPLETE:2f0s3h", "exit": 8, "counts": counts}, NOW)
    assert a.about == "The run ended with 2 failed, 0 skipped and 3 held tasks. Their results are missing."
    assert ("tasks", "1 done, 2 failed, 0 skipped, 3 held") in a.facts


def test_which_alert_gets_which_buttons():
    hung = alerts.run_alert(PROJECT, RUN, "hung", ["hung: no progress"], HB, NOW)
    assert [b[1] for b in hung.buttons] == ["stop:demo", "keep6:demo", "keep12:demo", "keep24:demo"]
    full = alerts.run_alert(PROJECT, RUN, "host_full", ["host_full: hostA below 1 GB free"], HB, NOW)
    assert [(b[0], b[2]) for b in full.buttons] == [("Stop", "edr stop b@demo --after-task --why host-full"),
                                                    ("Free space", "edr retire --host hostA --prune netlist --why host-full")]
    assert full.about.endswith("unless the disk gets back above the floor.") and "Free space removes" in full.todo[-1][0]
    bare = replace(PROJECT, stages={n: replace(st, prune={}) for n, st in PROJECT.stages.items()})
    assert [b[0] for b in alerts.run_alert(bare, RUN, "host_full", ["host_full: x"], HB, NOW).buttons] == ["Stop"]
    killing = replace(PROJECT, stages={**PROJECT.stages, "synth": replace(PROJECT.stages["synth"], budget=replace(
        PROJECT.stages["synth"].budget, kill=True))})
    for project, state, hb in ((PROJECT, "over_budget", {**HB, "phase": "OVER_BUDGET:synth", "exit": 9}),
                               (killing, "over_budget", HB), (PROJECT, "dead", HB), (PROJECT, "failed", {"phase": "FAILED:synth"})):
        assert alerts.run_alert(project, RUN, state, [], hb, NOW).buttons == [], state
    assert [(b[0], b[1]) for b in ALERTS["over_budget, ended"].buttons] == [("Continue", "continue:demo")]
    assert [(b[0], b[1]) for b in ALERTS["stopped"].buttons] == [("Continue", "continue:demo")]
    assert alerts.run_alert(PROJECT, ENDED, "stopped", [], {**HB, "phase": "STOPPED"}, NOW).todo == [
        ("See the last stage and the log tail:", "edr status b@demo")]


def test_an_alert_shows_the_last_four_log_lines_cut_to_a_phone():
    hb = {**HB, "last_log": "one\n\x1b[1;31mtwo\x1b[0m\nthree\n" + "x" * 150 + "\n\nfive\n"}
    assert alerts.run_alert(PROJECT, RUN, "dead", [], hb, NOW).log == ["two", "three", "x" * 99 + "…", "five"]

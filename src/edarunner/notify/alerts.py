"""What each alert says: one `Alert` per message, and its plain text for ntfy and mail.

The watcher builds the alert; `telegram.format.alert` renders it as HTML and `text` as plain
text, so every channel says the same thing: a title, what edarunner saw and why it matters,
the facts, and the command to run next.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from edarunner import board, hosts
from edarunner.model import Project
from edarunner.notify import Button, button_cmds
from edarunner.notify.telegram.format import MARK

Row = dict[str, Any]
CUT = 120  # a command line on a phone
LOG_WIDTH = 100  # a log line on a phone
LIMIT = 4000  # under the 4096 of Telegram and ntfy
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The states whose live run gets Stop and more time: the keep file holds off the watcher's own action.
KEEP_KINDS = frozenset({"hung", "looping", "over_budget", "superseded"})
KEEP_LINE = ("+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no "
             "automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; "
             "on Telegram it asks once more.")
# The buttons of an alert by the action of their `callback_data`: the label, the command line of a channel without
# buttons, and what the button does.
BUTTONS = {
    "stop": ("Stop", "edr stop {handle} --after-task --why {why}", "asks once more, then stops the run after its current task"),
    "keep6": ("+6h", "edr keep {handle} --hours 6", "6 more hours on the budget of the running stage or task, and for 6 "
                                                    "hours no kill as hung and no stop as superseded"),
    "keep12": ("+12h", "edr keep {handle} --hours 12", "the same for 12 hours"),
    "keep24": ("+24h", "edr keep {handle} --hours 24", "the same for 24 hours"),
    "free": ("Free space", "edr retire --host {host} --prune {prune} --why host-full",
             "asks once more, then removes the prune targets of the finished runs of the project on the full host; a "
             "run whose tree has stages left keeps them"),
    "continue": ("Continue", "edr continue {handle}", "asks once more, then runs the stages the tree of the run has "
                                                     "left as one new run on that tree"),
}


def button(action: str, project: str, **values: str) -> Button:
    """The alert button of `action` for `project`; `values` fill its command line."""
    label, cmd, _ = BUTTONS[action]
    return label, f"{action}:{project}", cmd.format(**values)


@dataclass
class Alert:
    """One alert: `title who` is the first line, `todo` holds (what it does, command or None).

    `log` holds the last lines of the log of the run, which every channel shows as one block."""

    kind: str
    key: str  # a channel keeps one message per kind and key
    title: str
    who: str
    about: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    code: str = ""
    todo: list[tuple[str, str | None]] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def cut(text: str, most: int = CUT) -> str:
    """`text` on one line, cut to `most` characters."""
    text = " ".join(str(text).split())
    return text if len(text) <= most else text[: most - 1] + "…"


def mark(a: Alert) -> str:
    """The mark of the state; the watcher alert is red."""
    return MARK.get(a.kind, "🔴")


def subject(project: str, a: Alert) -> str:
    """The first line of an alert as plain text."""
    return f"{mark(a)} {project}: {a.title}" + (f" {a.who}" if a.who else "")


def blocks(a: Alert, code: Callable[[str], str], plain: Callable[[str], str] = str,
           log: Callable[[list[str]], str] | None = None) -> str:
    """What was seen, the facts, the log lines, and what to do, a blank line apart; `code` renders a command,
    and `log` the log lines as one block."""
    facts = [plain(f"{k}: {v}") for k, v in a.facts] + ([code(a.code)] if a.code else [])
    todo = [line for say, cmd in a.todo for line in (plain(say), *([code(cmd)] if cmd else []))]
    shown = [(log or (lambda lines: "\n".join(map(code, lines))))(a.log)] if a.log else []
    return "\n\n".join("\n".join(b) for b in ([plain(a.about)], facts, shown, todo) if b)


def text(a: Alert, indent: str = "") -> str:
    """The body of an alert as plain text; `indent` sets a command and the log lines apart, as in a mail. A button
    is a command line."""
    buttons = [f"{label}: {c}" for label, c in button_cmds(a.buttons)]
    return "\n\n".join([blocks(a, lambda c: indent + c), *(["\n".join(buttons)] if buttons else [])])[:LIMIT]


def _stage(run: Row, hb: dict) -> str:
    step = hb.get("step", run.get("step"))
    at = " ".join(str(v) for v in (step, hb.get("step_name")) if v is not None and v != "")
    return str(hb.get("stage") or run.get("stage") or "-") + (f", step {at}" if at else "")


def log_lines(hb: dict) -> list[str]:
    """The last log lines of a heartbeat, without colour codes, each cut to LOG_WIDTH characters."""
    lines = [_ANSI.sub("", ln).rstrip() for ln in str(hb.get("last_log") or "").splitlines()]
    return [ln if len(ln) <= LOG_WIDTH else ln[:LOG_WIDTH - 1] + "…" for ln in lines if ln.strip()][-4:]


def _after(project: Project) -> str:
    return f"{board.hm(project.limits.grace_s)} after this alert"


def run_alert(project: Project, run: Row, state: str, reasons: list[str], hb: dict, now: float,
              left: list[str] | None = None, runs: list[Row] | None = None) -> Alert:
    """The alert of a run in `state`; `reasons` are what `watch.classify` found, `left` the stages that
    `edr continue` runs next on the tree of a run that ended, and `runs` the runs of the project, so the handle
    in the alert names this run alone.

    A live run that is `hung`, `looping`, `over_budget` or `superseded` gets Stop, +6h, +12h and +24h.
    A live run on a full host gets Stop, and Free space when the project declares prune targets; a
    keep does not hold off the full-host stop, since a full disk blocks every other user of the
    host. A run that ended `OVER_BUDGET` or `STOPPED` at the end of a stage, with stages left on its
    tree, gets Continue. Each alert says in one line what its buttons do. Mail and ntfy show each
    button as a command line. A run that ended `done` gets the opt-in alert `done`, without log lines."""
    h, lim = board.handle(run, runs), project.limits
    host = str(hb.get("host") or run.get("host") or "-")
    why = next((r.split(": ", 1)[1] for r in reasons if r.startswith(state + ": ")), "; ".join(reasons))
    cmd = board.triage_cmd(run, state, hb, runs)
    counts = hb.get("counts") or {}
    a = Alert(state, str(run.get("key") or run["run_id"]), state, h, why,
              [("stage", _stage(run, hb)), ("host", host)], log=log_lines(hb))
    status = ("See the last stage and the log tail:", f"edr status {h}")
    more = state in KEEP_KINDS
    if state == "dead":
        age, pid = board.hm(now - float(hb.get("updated") or now)), hb.get("driver_pid")
        a.title = "driver gone for"
        a.about = (f"The run has written no heartbeat for {age}, and its driver{f' {pid}' if pid else ''} is gone "
                   f"from {host}. Nothing runs until it is resumed.")
        a.todo = [("Resume it from the last step:", cmd),
                  ("edarunner resumes it once by itself when the stage has a resume command.", None)]
    elif state == "hung":
        a.title = "no progress in"
        a.about = (f"The run is alive, but it has shown {why}: the step, the log, the tree size and the CPU "
                   "time stand still. The tool may wait for a licence, or it is stuck.")
        a.todo = [("If it is stuck, stop it:", cmd),
                  (f"edarunner sends SIGTERM to its processes {_after(project)} unless it has a keep."
                   if lim.kill_hung else "edarunner leaves it alone, since kill_hung is off.", None)]
    elif state == "looping":
        a.title = "same failure again in"
        a.about = (f"The last {lim.streak} tasks failed with the same error, so the driver starts no new task. "
                   "More retries would fail the same way.")
        a.facts.append(("tasks", f"{counts.get('done', 0)} done, {counts.get('failed', 0)} failed"))
        a.todo = [("Read the log and fix the cause, then stop the run:", cmd)]
    elif state == "over_budget":
        name = hb.get("over_budget") or str(run.get("phase") or "").partition(":")[2] or hb.get("stage") or "-"
        st = project.stages.get(name)
        budget = st.budget if st else None
        limit = " and ".join(p for p in (f"{budget.hours:g} h" if budget and budget.hours else "",
                                          f"{budget.disk_gb:g} GB" if budget and budget.disk_gb else "") if p)
        a.title = "over budget in"
        a.about = f"Stage {name} went past its budget" + (f" of {limit}" if limit else "") + ". "
        if not board.is_live(hb):
            a.about += "The run ended OVER_BUDGET."
            a.todo = [status]
        elif budget and budget.kill:
            a.about += "The driver sent SIGTERM to the stage, and the run ends OVER_BUDGET."
            a.todo = [status]
            more = False  # more time comes too late for a stage that got SIGTERM
        else:
            a.about += "The stage runs to its end; more time on the budget before then lets the run go on."
            a.todo = [("Stop it now if the rest of the stage is of no use:", cmd)]
    elif state == "host_full":
        a.title, a.who = "disk almost full on", host
        a.facts[1] = ("run", h)
        a.about = (f"{host} has less than {hosts.floor(project.site, host):g} GB of free scratch, so the driver starts nothing "
                   f"new there, and the full disk blocks every other user of {host}. edarunner stops the newest run on "
                   f"{host} {_after(project)} unless the disk gets back above the floor.")
        a.todo = [("Free scratch on the host, or stop this run:", f"edr stop {h} --after-task --why host-full")]
    elif state == "superseded":
        a.title = "newer run replaces"
        newer = why.removeprefix("by ")
        a.about = (f"A newer run of the same label runs at another source{f' ({newer})' if newer else ''}. edarunner "
                   f"stops this run after its running task, {_after(project)}, unless it has a keep.")
        a.todo = [("To keep it running:", f"edr keep {h} --hours 12")]
    elif state == "held":
        a.title = "scheduler holds"
        a.about = "The scheduler holds the job, and it starts only after someone releases it."
        a.facts = [("job", str(run.get("handle") or "-"))]
        a.todo = [("Release it with the scheduler, or cancel it:", cmd)]
    elif state == "incomplete":
        a.title = "failed tasks in"
        a.about = (f"The run ended with {counts.get('failed', 0)} failed and {counts.get('skipped', 0)} skipped "
                   "tasks. Their results are missing.")
        a.facts.append(("tasks", f"{counts.get('done', 0)} done, {counts.get('failed', 0)} failed, "
                                 f"{counts.get('skipped', 0)} skipped"))
        a.todo = [("See which tasks failed and the log tail:", f"edr status {h}")]
    elif state == "failed":
        a.title = "failed run"
        where = str(run.get("phase") or "").partition(":")[2]
        code = hb.get("exit", run.get("exit"))
        a.about = (why[0].upper() + why[1:] + "." if why else
                   "The run ended" + (f" in stage {where}" if where else "") +
                   (f" with exit code {code}" if code is not None else "") + ".")
        a.todo = [("See the stage that failed and the log tail:", f"edr status {h}")]
    elif state == "killed":
        a.title = "killed run"
        by = hb.get("killed_by") or run.get("killed_by")
        a.about = "A signal ended the run" + (f" ({by})" if by else "") + ", so its last stage did not finish."
        a.todo = [status]
    elif state == "stopped":
        a.title = "stopped run"
        a.about = f"A stop ended the run in stage {hb.get('stage') or run.get('stage') or '-'}."
        a.todo = [status]
    elif state == "done":
        a.title, a.log = "run done", []
        a.about = "The run ended done" + (f" after {board.hm(hb['elapsed_s'])}" if hb.get("elapsed_s") else "") + "."
        a.todo = [("See its numbers:", f"edr metrics --run {h}")]
    if left:
        stages, root = board.join(left), str(hb.get("root") or run.get("root") or "-")
        a.about += f" It did not run {stages}."
        a.todo = [("Run the stages left on the same tree:", f"edr continue {h}"),
                  (f"Continue runs {stages} as one new run on {host}, in the tree {root}. On Telegram it asks once "
                   "more.", None)]
        a.buttons = [button("continue", project.project, handle=h)]
    if board.is_live(hb) and more:
        a.buttons = [button(x, project.project, handle=h, why=state) for x in ("stop", "keep6", "keep12", "keep24")]
        a.todo.append((KEEP_LINE, None))
    elif board.is_live(hb) and state == "host_full":
        prune = sorted({n for st in project.stages.values() for n in st.prune})
        a.buttons = [button("stop", project.project, handle=h, why="host-full")]
        if prune:
            a.buttons.append(button("free", project.project, host=host, prune=",".join(prune)))
        stop = "Stop ends the run after its current task"
        a.todo.append((f"{stop}, and Free space removes the prune targets {', '.join(prune)} of the finished runs of "
                       f"{project.project} on {host} whose trees have no stage left. On Telegram both ask once more."
                       if prune else
                       f"{stop}; on Telegram it asks once more.", None))
    return a


def metrics_alert(run: Row, text: str, runs: list[Row] | None = None) -> Alert:
    """The opt-in alert of the metric rows the watcher added to a run; `text` is that of the `metrics` event, such as
    `6 new: area_um2, wns_ns at pnr 8, 9`. A channel that edits keeps one message per run."""
    h = board.handle(run, runs)
    return Alert("metrics", str(run.get("key") or run["run_id"]), "new metrics of", h, text + ".",
                 todo=[("See them:", f"edr metrics --run {h}")])


def orphan_alert(project: Project | None, o: Row) -> Alert:
    """The alert of a tool process that no live run owns; `project` is the one it belongs to, None for none.

    `owner` is the run id of its `EDR_RUN_ID`, and `owner_handle` that run as `project/label@batch`."""
    host, pid, owner, state = o["host"], int(o["pid"]), o.get("owner"), o.get("owner_state")
    ssh = "" if host == "local" else f"ssh {host} "
    you = f"Your process {o['label']} runs on {host}"
    if not owner:
        title, about = "tool process with no run on", f"{you}, and no edarunner run owns it."
    elif state == "unknown":
        name = project.project if project else "a project"
        title, about = "tool process of an unknown run on", (f"{you} in the tree of the run {owner} of {name}, but "
                                                            "that project has no record of the run.")
    else:
        ended = {"dead": "whose driver is gone", "retired": "which was retired", "abandoned": "which was retired",
                 "stopped": "which was stopped"}.get(state, f"which has ended ({str(state).replace('_', ' ')})")
        title, about = "tool process of an ended run on", f"{you} for the run {o['owner_handle']}, {ended}."
    return Alert(
        "orphan", o["key"], title, host, about + " It may hold a licence seat.",
        [("process", f"{o['label']}, pid {pid}"), ("running for", board.hm(o.get("etimes"))),
         ("directory", o.get("cwd") or "-")],
        cut(o["phase"]),
        [("Check it:", f"{ssh}ps -o pid,etime,args -p {pid}"),
         ("If it is yours and stale, end it:", f"{ssh}kill {pid}"),
         (f"edarunner sends it SIGTERM {_after(project)}, since kill_orphan is on." if project and project.limits.kill_orphan
          else "edarunner never kills it, since kill_orphan is off." if project
          else "edarunner never kills a process that belongs to no project.", None)])


def clock_alert(host: str, skew: float) -> Alert:
    """The alert of a host whose clock is off from the head node's by `skew` seconds."""
    return Alert(
        "clock", host, "clock off on", host,
        f"The clock of {host} is {abs(skew):.0f} s {'ahead of' if skew > 0 else 'behind'} the head node. A heartbeat and a "
        "seat lease carry the time of the host, so the stale, dead and lease windows move by as much.",
        [("skew", f"{skew:+.0f} s")],
        todo=[("Ask the admins to sync the host with NTP. edr hosts names every host whose clock is off.", None)])


def config_alert(project: Project, error: str, watched: bool = True) -> Alert:
    """The alert of an edr.toml, tasks.toml or site file that stopped loading; `watched` when a watcher goes on
    with the last config that loaded, else no watcher runs until the file loads."""
    about = ("The watcher goes on with the last config that loaded. It reads the heartbeats, sends the alerts and "
             "collects the results, but it resumes and launches nothing until the file loads again." if watched else
             "No watcher runs for the project until the file loads, so its runs get no alert, no collect and no resume.")
    return Alert("config", project.project, "config does not load", "", about, [("project", str(project.root))],
                 cut(error), [("See every problem of the project files:", "edr check")])


def served_alert(project: Project, title: str, what: str) -> Alert:
    """The alert of the supervisor about the watcher of `project`: `what` happened to it."""
    return Alert("watch", project.project, title, project.project, f"The watcher of {project.project} {what}.",
                 [("project", str(project.root))],
                 todo=[("Its log is in the journal of the supervisor:", "journalctl --user -u edr-serve")])


def serve_alert(age: float | None, pid: object) -> Alert:
    """The alert of a supervisor that has not finished a cycle for `age` seconds; None for no serve.json."""
    return Alert(
        "watch", "serve", "supervisor stopped", "",
        ("The supervisor has never finished a cycle." if age is None else
         f"The supervisor has not finished a cycle for {board.hm(age)}.") + " No project is watched until it runs again.",
        [("pid", str(pid or "-"))],
        todo=[("See why it stopped:", "systemctl --user status edr-serve"), ("Start it again:", "systemctl --user restart edr-serve")])


def watch_alert(project: Project, age: float | None, pid: object) -> Alert:
    """The alert of a watcher that has not finished a cycle for `age` seconds; None for no watch.json."""
    return Alert(
        "watch", "", "watcher stopped", "",
        ("The watcher has never finished a cycle." if age is None else
         f"The watcher has not finished a cycle for {board.hm(age)}.") + " No alert arrives until it runs again.",
        [("pid", str(pid or "-")), ("project", str(project.root))],
        todo=[("Run one cycle that writes nothing; it prints the error that stops the watcher:", "edr watch --dry-run"),
              ("Then restart the watcher service.", None)])

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

from edarunner import board
from edarunner.model import Project
from edarunner.notify import Button, alert_buttons, button_cmds
from edarunner.notify.telegram.format import MARK

Row = dict[str, Any]
CUT = 120  # a command line or a log line on a phone
LIMIT = 4000  # under the 4096 of Telegram and ntfy
# The kinds whose run still reads a keep or a stop file, so the three buttons act.
BUTTON_KINDS = frozenset({"hung", "looping", "over_budget", "host_full", "superseded"})
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class Alert:
    """One alert: `title who` is the first line, `todo` holds (what it does, command or None)."""

    kind: str
    key: str  # a channel keeps one message per kind and key
    title: str
    who: str
    about: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    code: str = ""
    todo: list[tuple[str, str | None]] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)


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


def blocks(a: Alert, code: Callable[[str], str], plain: Callable[[str], str] = str) -> str:
    """What was seen, the facts, and what to do, a blank line apart; `code` renders a command or a log line."""
    facts = [plain(f"{k}: {v}") for k, v in a.facts] + ([code(a.code)] if a.code else [])
    todo = [line for say, cmd in a.todo for line in (plain(say), *([code(cmd)] if cmd else []))]
    return "\n\n".join("\n".join(b) for b in ([plain(a.about)], facts, todo) if b)


def text(a: Alert, indent: str = "") -> str:
    """The body of an alert as plain text; `indent` sets a code line apart, as in a mail. A button is a command line."""
    buttons = [f"{label}: {c}" for label, c in button_cmds(a.buttons)]
    return "\n\n".join([blocks(a, lambda c: indent + c), *(["\n".join(buttons)] if buttons else [])])[:LIMIT]


def _stage(run: Row, hb: dict) -> str:
    step = hb.get("step", run.get("step"))
    at = " ".join(str(v) for v in (step, hb.get("step_name")) if v is not None and v != "")
    return str(hb.get("stage") or run.get("stage") or "-") + (f", step {at}" if at else "")


def _last_log(hb: dict) -> str:
    line = next((ln for ln in reversed(str(hb.get("last_log") or "").splitlines()) if ln.strip()), "")
    return cut(_ANSI.sub("", line))


def _after(project: Project) -> str:
    return f"{board.hm(project.limits.grace_s)} after this alert"


def run_alert(project: Project, run: Row, state: str, reasons: list[str], hb: dict, now: float) -> Alert:
    """The alert of a run in `state`; `reasons` are what `watch.classify` found."""
    h, lim = board.handle(run), project.limits
    host = str(hb.get("host") or run.get("host") or "-")
    why = next((r.split(": ", 1)[1] for r in reasons if r.startswith(state + ": ")), "; ".join(reasons))
    cmd = board.triage_cmd(run, state, hb)
    counts = hb.get("counts") or {}
    a = Alert(state, str(run.get("key") or run["run_id"]), state, h, why,
              [("stage", _stage(run, hb)), ("host", host)], _last_log(hb),
              buttons=alert_buttons(h) if state in BUTTON_KINDS and run.get("run_id") else [])
    status = ("See the last stage and the log tail:", f"edr status {h}")
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
                  (f"edarunner sends SIGTERM to its processes {_after(project)} unless you press ack."
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
        else:
            a.about += ("The driver sent SIGTERM to the stage, and the run ends OVER_BUDGET."
                        if budget and budget.kill else "The stage runs to its end, and the run then ends OVER_BUDGET.")
            a.todo = [("Stop it now if the rest of the stage is of no use:", cmd)]
    elif state == "host_full":
        a.title, a.who = "disk almost full on", host
        a.facts[1] = ("run", h)
        a.about = (f"{host} has less than {lim.host_free_min_gb:g} GB of free scratch, so the driver starts nothing "
                   f"new there. edarunner stops the newest run on {host} {_after(project)} unless that run has an ack.")
        a.todo = [("Free scratch on the host. To keep this run from the stop, ack it:", f"edr keep {h} --ack")]
    elif state == "superseded":
        a.title = "newer run replaces"
        newer = why.removeprefix("by ")
        a.about = (f"A newer run of the same label runs at another source{f' ({newer})' if newer else ''}. edarunner "
                   f"stops this run after its running task, {_after(project)}, unless it has a keep file.")
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
    return a


def orphan_alert(project: Project, o: Row) -> Alert:
    """The alert of a tool process that no live run owns; `owner` is the run of its `EDR_RUN_ID`, when it has one."""
    host, pid, owner, state = o["host"], int(o["pid"]), o.get("owner"), o.get("owner_state")
    ssh = "" if host == "local" else f"ssh {host} "
    you = f"Your process {o['label']} runs on {host}"
    if not owner:
        title, about = "tool process with no run on", f"{you}, and no edarunner run owns it."
    elif state == "unknown":
        title, about = "tool process of an unknown run on", f"{you} in the tree of the run {owner}, but this project has no record of that run."
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
         (f"edarunner sends it SIGTERM {_after(project)}, since kill_orphan is on." if project.limits.kill_orphan
          else "edarunner never kills it, since kill_orphan is off.", None)])


def watch_alert(project: Project, age: float | None, pid: object) -> Alert:
    """The alert of a watcher that has not finished a cycle for `age` seconds; None for no watch.json."""
    return Alert(
        "watch", "", "watcher stopped", "",
        ("The watcher has never finished a cycle." if age is None else
         f"The watcher has not finished a cycle for {board.hm(age)}.") + " No alert arrives until it runs again.",
        [("pid", str(pid or "-")), ("project", str(project.root))],
        todo=[("Run one cycle that writes nothing; it prints the error that stops the watcher:", "edr watch --dry-run"),
              ("Then restart the watcher service.", None)])

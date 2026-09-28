"""Telegram HTML from database rows: the marks, the run line, the board, the lists and the alert.

Every function is pure and returns text under the message limit of 4096 characters.
"""

from __future__ import annotations

import html
import textwrap
import time
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from edarunner import board as runs

if TYPE_CHECKING:
    from edarunner.notify.alerts import Alert

Row = dict[str, Any]

LIMIT = 4000  # the message limit is 4096 characters after parsing
# One mark per state for a bot message.
MARK = {"running": "🟢", "queued": "🔵", "resumed": "🔵", "stale": "🟡", "host_full": "🟡", "superseded": "🟡",
        "dead": "🔴", "hung": "🔴", "looping": "🔴", "over_budget": "🔴", "orphan": "🔴", "failed": "🔴", "killed": "🔴",
        "incomplete": "🟠", "pending": "🔵", "held": "🟠", "suspended": "🟡", "done": "⚪", "retired": "⚫", "stopped": "⚫", "imported": "⚫", "abandoned": "⚫"}


def esc(text: object) -> str:
    """`text` escaped for parse_mode HTML."""
    return html.escape(str(text), quote=False)


def pre(text: str) -> str:
    """The last 4000 characters of `text` as an HTML <pre> block."""
    return "<pre>" + esc(str(text)[-LIMIT:]) + "</pre>"


def fit(text: str) -> str:
    """`text` cut at a line end to the message limit; a line holds no open tag."""
    return text if len(text) <= LIMIT else text[:LIMIT].rsplit("\n", 1)[0] + "\n…"


def mark(state: str) -> str:
    """The mark of a run state; an unknown state gets the mark of done."""
    return MARK.get(state, "⚪")


def head(project: str, title: str) -> str:
    """The bold first line of every message: the project, then `title`."""
    return f"<b>{esc(project)}: {esc(title)}</b>"


def alert(project: str, a: Alert) -> str:
    """An alert: the mark and the bold title, then what was seen, the facts, the log lines in a <pre> block and each
    command in monospace."""
    from edarunner.notify.alerts import blocks

    title = f"{MARK.get(a.kind, '🔴')} <b>{esc(project)}: {esc(a.title)}</b>" + (f" <code>{esc(a.who)}</code>" if a.who else "")
    return fit(title + "\n" + blocks(a, lambda c: f"<code>{esc(c)}</code>", esc, lambda lines: pre("\n".join(lines))))


# A state in plain words, for the legend under a board.
WORDS = {"stale": "stale (no heartbeat for a while)", "dead": "dead (driver gone)", "hung": "hung (no progress)",
         "looping": "looping (the same failure again)", "over_budget": "over budget", "host_full": "host disk full",
         "superseded": "replaced by a newer run", "pending": "waiting in the scheduler", "held": "held by the scheduler",
         "suspended": "suspended by the scheduler", "incomplete": "incomplete (some tasks failed)",
         "stopped": "stopped by hand", "abandoned": "retired while live"}
DAY_S = 86400


def where(row: Row, totals: dict[str, int] | None = None, names: dict[int, str] | None = None) -> str:
    """`stage step/total name`; `stage, starting` for a stage with steps before its first step."""
    stage, step, totals = row.get("stage"), row.get("step"), totals or {}
    if not stage:
        return ""
    if step is None:
        return f"{stage}, starting" if stage in totals else str(stage)
    return f"{stage} {step}" + (f"/{totals[stage]}" if totals.get(stage) else "") + (
        f" {names[step]}" if (names or {}).get(step) else "")


def run_line(row: Row, now: float, totals: dict[str, int] | None = None, names: dict[int, str] | None = None) -> str:
    """A live run: `mark handle state, stage step/total name, runtime`; a running run leaves out its state.
    A finished run: `mark handle state, ended <when>`."""
    st = runs.state_of(row)
    if runs.is_live(row):
        parts = [st if st != "running" else "", where(row, totals, names),
                 runs.hm(now - row["started"]) if row.get("started") else ""]
    else:
        end = row.get("updated")
        parts = [st, "" if end is None else "ended " + (runs.hm(now - end) + " ago" if now - end < DAY_S
                                                      else time.strftime("%d.%m %H:%M", time.localtime(end)))]
    return f"{mark(st)} <code>{esc(runs.handle(row))}</code> {esc(', '.join(p for p in parts if p))}".rstrip()


def legend(states: Iterable[str]) -> str:
    """Each mark of `states` in plain words, in the order the marks first appear."""
    words: dict[str, list[str]] = {}
    for st in states:
        w = words.setdefault(mark(st), [])
        if WORDS.get(st, st) not in w:
            w.append(WORDS.get(st, st))
    return ", ".join(f"{m} {' or '.join(w)}" for m, w in words.items())


def board(rows: list[Row], now: float | None = None, totals: dict[str, int] | None = None,
          names: dict[int, str] | None = None, everything: bool = False, most: int = 30) -> str:
    """The live runs, then the runs that ended in the last day (with `everything`, every run that ended),
    then the count per state and a legend of the marks, in italics."""
    now = now or time.time()
    ordered = runs.order(rows)
    if not ordered:
        return "<i>no runs</i>"
    live = [r for r in ordered if runs.is_live(r)]
    ended = [r for r in ordered if not runs.is_live(r)]
    recent = ended if everything else [r for r in ended if now - (r.get("updated") or 0) < DAY_S]
    lines = ["<b>Running</b>", *(run_line(r, now, totals, names) for r in live[:most])]
    if len(live) > most:
        lines.append(f"<i>… and {len(live) - most} more</i>")
    if not live:
        lines.append("<i>nothing live</i>")
    if ended:
        lines += ["", "<b>" + ("Finished" if everything else "Finished in the last 24 hours") + "</b>",
                  *(run_line(r, now) for r in recent[:most])]
        if len(recent) > most:
            lines.append(f"<i>… and {len(recent) - most} more</i>")
        if older := len(ended) - len(recent):
            lines.append(f"<i>and {older} older run{'s' if older != 1 else ''}: /status all</i>")
    counts = Counter(runs.state_of(r) for r in ordered)
    shown = [runs.state_of(r) for r in live[:most] + recent[:most]]
    lines += ["", "<i>" + esc(", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: runs.RANK.get(kv[0], 7))))
              + "</i>", "<i>" + esc(legend(shown)) + "</i>"]
    return fit("\n".join(lines))


def run_detail(row: Row, hb: dict, now: float, everyone: list[Row] | None = None) -> str:
    """The state, stage, step, host, age, next command and the last log lines of one run; `everyone`, the runs of
    the project, makes its handle name this run alone."""
    from edarunner.notify.alerts import log_lines

    state = runs.state_of(row)
    step = " ".join(str(v) for v in (hb.get("step") or row.get("step"), hb.get("step_name")) if v not in (None, ""))
    age = runs.hm(None if row.get("updated") is None else now - row["updated"])
    cmd = runs.triage_cmd(row, state, hb, everyone)
    lines = [f"{mark(state)} <code>{esc(runs.handle(row, everyone))}</code> {esc(state)}",
             esc(f"stage {row.get('stage') or '-'}, step {step or '-'}"), esc(f"on {row.get('host') or '-'}, {age}")]
    lines += [f"<code>{esc(cmd)}</code>"] if cmd else []
    return "\n".join(lines + [pre("\n".join(log_lines(hb)) or "-")])


def events(rows: Iterable[Row], names: dict[str, str]) -> str:
    """Newest first: `HH:MM kind handle`, the reason indented under it in italics, run ids replaced by handles."""
    lines = []
    for e in reversed(list(rows)):
        text = str(e["text"] or "")
        for run_id, h in names.items():
            text = text.replace(run_id, h)
        who = names.get(e["run_id"])
        lines.append(f"{time.strftime('%H:%M', time.localtime(e['ts']))} <b>{esc(e['kind'])}</b>"
                     + (f" <code>{esc(who)}</code>" if who else ""))
        if text and text != e["kind"]:
            lines.append(f"    <i>{esc(textwrap.shorten(text, 200, placeholder=' …'))}</i>")
    return fit("\n".join(lines)) or "<i>no events</i>"


def room(r: Row) -> str:
    """The free room of a host row of census.host_view, of total: cores, RAM, scratch and the idle GPUs."""
    parts = [f"{r['free_cores']:g}/{r['cores']} cores", f"{r['free_ram_gb']:.0f}/{r['total_ram_gb']:.0f} GB RAM",
             f"{r['free_gb']:.0f}/{r['total_gb']:.0f} GB scratch"]
    return "free " + ", ".join(parts + ([f"{r['gpus_idle']}/{r['gpus']} GPUs"] if r["gpus"] else []))


def yours(r: Row) -> str:
    """Your runs of a host row by project, with the cores and the scratch they use."""
    if not r["runs"]:
        return "none of yours"
    return "yours: " + ", ".join(f"{p} {n}" for p, n in sorted(r["runs"].items())) + (
        f", {r['our_cores']:g} cores, {r['our_gb']:g} GB scratch")


def hosts(rows: Iterable[Row]) -> str:
    """One line per host of census.host_view: whether a run can start, the free room and your runs; under it in
    italics why no run can start there and what your runs fill."""
    lines = []
    for r in rows:
        mark = runs.START[r["start"]]
        if "error" in r:
            lines.append(f"{mark} <b>{esc(r['host'])}</b> <i>no answer</i>")
            continue
        lines.append(f"{mark} <b>{esc(r['host'])}</b> {esc(room(r))}; {esc(yours(r))}")
        lines += [f"    <i>{esc(t)}</i>" for t in (r["why"], r["note"]) if t]
    return fit("\n".join(lines + ["", "<i>🟢 a run can start, 🔴 no run can start, ⚫ no answer</i>"])) if lines else "<i>no hosts</i>"


def global_board(projects: dict[str, list[Row]], labels: dict[str, tuple[dict, dict]], hosts: list[Row], now: float,
                 most: int = 10) -> str:
    """The board of every project: the live runs of each, at most `most`, and one line for a project with none;
    the hosts that hold your runs, with their free room and what your runs fill; and the count of live runs.

    `labels` holds the step totals and the step names of each project."""
    lines: list[str] = []
    states: Counter = Counter()
    for name, rows in sorted(projects.items()):
        live = [r for r in runs.order(rows) if runs.is_live(r)]
        first = sum(states.values()) + 1  # `#n` counts the live runs over every project, as the bot resolves it
        states.update(runs.state_of(r) for r in live)
        if not live:
            ended = sum(1 for r in rows if not runs.is_live(r) and now - (r.get("updated") or 0) < DAY_S)
            lines.append(f"<b>{esc(name)}</b> <i>nothing live" + (f", {ended} ended in 24 h" if ended else "") + "</i>")
            continue
        totals, names = labels.get(name, ({}, {}))
        lines += [f"<b>{esc(name)}</b>", *(f"#{n} {run_line(r, now, totals, names)}" for n, r in enumerate(live[:most], first))]
        if len(live) > most:
            lines.append(f"<i>… and {len(live) - most} more: /status {esc(name)}</i>")
    if hosts:
        lines += ["", "<b>Machines</b>"]
        for h in hosts:
            lines.append(f"<b>{esc(h['host'])}</b> {esc(room(h))}; {esc(yours(h))}")
            lines += [f"    <i>{esc(h['note'])}</i>"] if h["note"] else []
    count = ", ".join(f"{n} {s}" for s, n in sorted(states.items(), key=lambda kv: runs.RANK.get(kv[0], 7)))
    lines += ["", "<i>" + esc(f"{sum(states.values())} live" + (f": {count}" if count else "")
                              + ". /status <project> shows one project.") + "</i>"]
    return fit("\n".join(lines))


def projects(rows: Iterable[Row]) -> str:
    """One line per project of `edr projects`: its watcher and its live runs, and its note in italics."""
    lines = []
    for r in rows:
        watcher = f"watched by pid {r['watcher']}" if r.get("watcher") else "not watched"
        live = "" if r.get("live") is None else f", {r['live']} live"
        lines.append(f"<b>{esc(r['project'])}</b> {watcher}{live}" + (f"\n    <i>{esc(r['note'])}</i>" if r.get("note") else ""))
    return fit("\n".join(lines)) or "<i>no registered project</i>"


def tools(rows: Iterable[Row]) -> str:
    """`tool used/total seats used, host, host` per tool; a failed probe shows its note."""
    lines = []
    for r in rows:
        if "note" in r:
            seats = f"<i>{esc(r['note'])}</i>"
        elif "free" in r and "total" in r:
            seats = f"{r['total'] - r['free']}/{r['total']} seats used"
        elif "free" in r:
            seats = f"{r['free']} seats left"
        else:
            seats = f"{r['total']} seats" if "total" in r else ""
        lines.append(f"<b>{esc(r['tool'])}</b> " + ", ".join(filter(None, [seats, *map(esc, r["hosts"])])))
    return fit("\n".join(ln.rstrip() for ln in lines)) or "<i>no tools</i>"


def help_text(groups: dict[str, list[tuple[str, str]]]) -> str:
    """Prose: a bold line per group, then one `usage: help` line per command."""
    return fit("\n\n".join(f"<b>{esc(g)}</b>\n" + "\n".join(f"{esc(u.rstrip())}: {esc(h)}" for u, h in cmds)
                           for g, cmds in groups.items()))


def digest(name: str, ended: list[Row], live: list[Row], queued: list[Row], alerts: list[Row], now: float,
           most: int = 10) -> str:
    """The digest of one project: the runs that ended, the live runs with their age, the queue and the open
    alerts, each part with a run, at most `most` lines each; one line for a project with none of them."""
    if not ended and not live and not queued:
        return f"<b>{esc(name)}</b> <i>nothing ended, nothing live</i>"

    def line(r: Row, *parts: str) -> str:
        return f"{mark(runs.state_of(r))} <code>{esc(runs.handle(r))}</code> {esc(', '.join(p for p in parts if p))}".rstrip()

    parts = {"ended": [line(r, runs.state_of(r)) for r in ended],
             "live": [line(r, str(r.get("stage") or ""), runs.hm(now - r["started"]) if r.get("started") else "")
                      for r in live],
             "queued": [line(r) for r in queued],
             "open alerts": [line(r, runs.state_of(r)) for r in alerts]}
    return f"<b>{esc(name)}</b>\n" + "\n".join(
        f"<i>{title}</i>\n" + "\n".join(lines[:most] + ([f"<i>… and {len(lines) - most} more</i>"] if len(lines) > most else []))
        for title, lines in parts.items() if lines)

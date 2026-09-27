"""Telegram HTML from database rows: the marks, the run line, the board, the lists and the alert.

Every function is pure and returns text under the message limit of 4096 characters.
"""

from __future__ import annotations

import html
import re
import textwrap
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

from edarunner import board as runs

Row = dict[str, Any]

LIMIT = 4000  # the message limit is 4096 characters after parsing
# One mark per state for a bot message.
MARK = {"running": "🟢", "queued": "🔵", "resumed": "🔵", "stale": "🟡", "host_full": "🟡", "superseded": "🟡",
        "dead": "🔴", "hung": "🔴", "looping": "🔴", "over_budget": "🔴", "orphan": "🔴", "failed": "🔴", "killed": "🔴",
        "incomplete": "🟠", "done": "⚪", "retired": "⚫", "stopped": "⚫", "imported": "⚫", "abandoned": "⚫"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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


def alert(project: str, text: str, cmd: str | None = None) -> str:
    """The first line of `text` as the title, the rest as prose, `cmd` in monospace.

    A title `state handle` gets the mark of the state and the handle in monospace.
    """
    title, _, rest = text.partition("\n")
    state, _, who = title.partition(" ")
    top = f"{MARK[state]} {head(project, state)} <code>{esc(who)}</code>" if state in MARK and who else head(project, title)
    return top + (f"\n{esc(rest)}" if rest else "") + (f"\n<code>{esc(cmd)}</code>" if cmd else "")


def run_line(row: Row, now: float, totals: dict[str, int] | None = None) -> str:
    """`mark handle state, stage step/total, age`; a running run leaves out its state."""
    st, parts = runs.state_of(row), []
    if st != "running":
        parts.append(st)
    if runs.is_live(row) and row.get("stage"):
        step = "" if row.get("step") is None else f" {row['step']}" + (
            f"/{totals[row['stage']]}" if (totals or {}).get(row["stage"]) else "")
        parts.append(f"{row['stage']}{step}")
    age = None if row.get("updated") is None else max(0.0, now - row["updated"])
    parts.append(runs.hm(age))
    return f"{mark(st)} <code>{esc(runs.handle(row))}</code> {esc(', '.join(parts))}"


def board(rows: list[Row], now: float | None = None, totals: dict[str, int] | None = None, most: int = 30) -> str:
    """One run line per run in board order, then the count per state in italics."""
    now = now or time.time()
    ordered = runs.order(rows)
    lines = [run_line(r, now, totals) for r in ordered[:most]]
    if len(ordered) > most:
        lines.append(f"<i>… and {len(ordered) - most} more</i>")
    if ordered and not any(runs.is_live(r) for r in ordered):
        lines.append("<i>nothing live</i>")
    counts = Counter(runs.state_of(r) for r in ordered)
    lines.append("<i>" + (", ".join(f"{v} {esc(k)}" for k, v in sorted(counts.items(), key=lambda kv: runs.RANK.get(kv[0], 7)))
                          or "no runs") + "</i>")
    return fit("\n".join(lines))


def run_detail(row: Row, hb: dict, now: float) -> str:
    """The state, stage, step, host, age, next command and last log line of one run."""
    state = runs.state_of(row)
    step = " ".join(str(v) for v in (hb.get("step") or row.get("step"), hb.get("step_name")) if v not in (None, ""))
    age = runs.hm(None if row.get("updated") is None else now - row["updated"])
    log_line = next((ln for ln in reversed(str(hb.get("last_log") or "").splitlines()) if ln.strip()), "-")
    cmd = runs.triage_cmd(row, state, hb)
    lines = [f"{mark(state)} <code>{esc(runs.handle(row))}</code> {esc(state)}",
             esc(f"stage {row.get('stage') or '-'}, step {step or '-'}"), esc(f"on {row.get('host') or '-'}, {age}")]
    lines += [f"<code>{esc(cmd)}</code>"] if cmd else []
    return "\n".join(lines + [pre(_ANSI.sub("", log_line)[-300:])])


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


def resources(probe: Row) -> list[tuple[str, str]]:
    """The resources of one host probe as (name, `used/total`), in the order of the /hosts line."""
    cores = probe["cores"]
    out = [("cores", f"{max(0, min(cores, round(probe['load'])))}/{cores}"),
           ("ram", f"{probe['total_ram_gb'] - probe['free_ram_gb']:.0f}/{probe['total_ram_gb']:.0f} GB"),
           ("scratch", f"{probe['total_gb'] - probe['free_gb']:.0f}/{probe['total_gb']:.0f} GB")]
    if probe["gpus"]:
        out.append(("gpu", f"{probe['gpus'] - probe['gpus_idle']}/{probe['gpus']}"))
    return out


def hosts(rows: Iterable[Row]) -> str:
    """`host mark cores used/total, mark ram used/total GB, …` per host; `marks` of a row holds the marks."""
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{runs.NO_ANSWER} <b>{esc(r['host'])}</b> <i>no answer</i>")
            continue
        lines.append(f"<b>{esc(r['host'])}</b> " + ", ".join(f"{r['marks'][name]} {name} {value}"
                                                              for name, value in resources(r)))
    return fit("\n".join(lines)) or "<i>no hosts</i>"


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


def plain(text: str) -> str:
    """Telegram HTML as terminal text: no tags, no entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _section(title: str, lines: list[str], most: int = 10) -> str:
    body = lines[:most] + ([f"<i>… and {len(lines) - most} more</i>"] if len(lines) > most else [])
    return f"<b>{esc(title)}</b>\n" + ("\n".join(body) or "<i>none</i>")


def digest(ended: list[Row], live: list[Row], queued: list[Row], hosts: list[Row], alerts: list[Row],
           since: float, now: float) -> str:
    """The daily summary: the runs that ended since `since`, the live runs with their age, the queue,
    the hosts with the least free scratch, and the open alerts, one section each."""
    def line(r: Row, *parts: str) -> str:
        st = runs.state_of(r)
        return f"{mark(st)} <code>{esc(runs.handle(r))}</code> {esc(', '.join(p for p in parts if p))}".rstrip()

    return fit("\n\n".join([
        _section("Ended since " + time.strftime("%d.%m %H:%M", time.localtime(since)),
                 [line(r, runs.state_of(r)) for r in ended]),
        _section("Live", [line(r, str(r.get("stage") or ""), runs.hm(now - r["started"]) if r.get("started") else "")
                          for r in live]),
        _section("Queued", [line(r) for r in queued]),
        _section("Least free scratch", [f"<b>{esc(h['host'])}</b> scratch {h['total_gb'] - h['free_gb']:.0f}/"
                                        f"{h['total_gb']:.0f} GB" for h in hosts]),
        _section("Open alerts", [line(r, runs.state_of(r)) for r in alerts]),
    ]))

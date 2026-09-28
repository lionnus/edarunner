"""The daily digest of every registered project: what ended, what runs, what waits and what is open.

The holder of `serve.lock` sends it once a day at `digest_at` of the user file; `/digest`,
`edr status --digest` and `edr notify --digest` show the same text on demand. The day and the
time of the last digest live in `~/.edr/store.json`.
"""

from __future__ import annotations

import time

from edarunner import board, config
from edarunner.db import Database
from edarunner.model import Project
from edarunner.notify.telegram import format as fmt

DAY_S = 86400


def since(last: dict, now: float) -> float:
    """The time of the last digest, `last` as the store keeps it, or a day ago."""
    return float(last.get("ts") or now - DAY_S)


def due(at: str, last: dict, now: float) -> bool:
    """True once a day, from `at` local time on."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    return bool(at) and time.strftime("%H:%M", time.localtime(now)) >= at and last.get("day") != day


def text(projects: dict[str, Project], start: float, now: float) -> str:
    """The digest as Telegram HTML: a block per project with a run that ended since `start` or is live, and one
    line per other project."""
    blocks = [f"<i>since {time.strftime('%d.%m %H:%M', time.localtime(start))}</i>"]
    for name, project in sorted(projects.items()):
        path = project.data / "edr.db"
        try:
            if not path.exists():
                blocks.append(fmt.digest(name, [], [], [], [], now))
                continue
            with Database(path) as db:
                blocks.append(_block(name, project, db, start, now))
        except Exception as e:  # one database that does not open must not hide the others
            blocks.append(f"<b>{fmt.esc(name)}</b> <i>{fmt.esc(f'no digest: {e}')}</i>")
    return fmt.fit("\n\n".join(blocks))


def _block(name: str, project: Project, db: Database, start: float, now: float) -> str:
    retired = {b["batch"] for b in db.batches() if b.get("retired")}
    rows = board.order([r for r in db.runs() if r["batch"] not in retired])
    live = [r for r in rows if board.is_live(r)]
    ended = [r for r in rows if not board.is_live(r) and (r.get("updated") or 0) >= start]
    notes = db.get_store("notified", {})
    alerted = {k for k, n in notes.items() if n.get("state") in (n.get("msgs") or {})}  # an alert for its state now
    alerts = [r for r in live if r["run_id"] in alerted and not config.kept(project, r, now)]
    return fmt.digest(name, ended, [r for r in live if r.get("state") != "queued"],
                      [r for r in live if r.get("state") == "queued"], alerts, now)

"""The daily digest of one project: what ended, what runs, what waits, where scratch is short, what is open.

The watcher sends it once a day at `limits.digest_at`; `/digest` and `edr status --digest`
show the same text on demand. The day of the last digest lives in the ledger's kv table.
"""

from __future__ import annotations

import time

from edarunner import board, config
from edarunner.ledger import Ledger
from edarunner.model import Project
from edarunner.notify.telegram import format as fmt

DAY_S = 86400
HOSTS = 3  # hosts in the scratch section


class Digest:
    """The digest of one project, from the ledger and the board.json of the last watcher cycle."""

    def __init__(self, project: Project, ledger: Ledger) -> None:
        self.project = project
        self.ledger = ledger

    def text(self, now: float) -> str:
        """The digest as Telegram HTML."""
        last = self.ledger.get_kv("digest", {})
        since = float(last.get("ts") or now - DAY_S)
        retired = {b["batch"] for b in self.ledger.batches() if b.get("retired")}
        rows = board.order([r for r in self.ledger.runs() if r["batch"] not in retired])
        live = [r for r in rows if board.is_live(r)]
        ended = [r for r in rows if not board.is_live(r) and (r.get("updated") or 0) >= since]
        notes = self.ledger.get_kv("notified", {})
        alerts = [r for r in live if self._alerted(notes.get(r["run_id"]) or {}) and not self._acked(r)]
        probes = config.load_json(self.project.data / "board" / "board.json").get("hosts") or {}
        hosts = sorted((p for p in probes.values() if "error" not in p and p.get("total_gb")),
                       key=lambda p: p["free_gb"])[:HOSTS]
        return fmt.digest(ended, [r for r in live if r.get("state") != "queued"],
                          [r for r in live if r.get("state") == "queued"], hosts, alerts, since, now)

    def due(self, now: float) -> bool:
        """True once a day, from `digest_at` local time on."""
        at = self.project.limits.digest_at
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        return bool(at) and time.strftime("%H:%M", time.localtime(now)) >= at and self.ledger.get_kv(
            "digest", {}).get("day") != day

    def mark_sent(self, now: float) -> None:
        """Record today as sent, and `now` as the start of the next digest."""
        self.ledger.set_kv("digest", {"day": time.strftime("%Y-%m-%d", time.localtime(now)), "ts": now})

    @staticmethod
    def _alerted(note: dict) -> bool:
        """True when the watcher sent an alert for the state the run is in now."""
        return note.get("state") in (note.get("msgs") or {})

    def _acked(self, row: dict) -> bool:
        keep = config.load_json(self.project.state / str(row["batch"]) / f"{row['run_id']}.keep.json")
        return bool(keep.get("ack"))

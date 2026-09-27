"""The ntfy channel: one push message per alert over HTTP, with a priority by alert kind."""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

from edarunner.model import Ntfy
from edarunner.notify import Button, Notifier, plain, untag

log = logging.getLogger(__name__)

# ntfy priorities: 5 urgent, 4 high, 3 default, 2 low, 1 min. An alert kind not listed is high.
PRIORITY = {"dead": 5, "failed": 5, "killed": 5, "host_full": 5, "watch": 5, "superseded": 3, "note": 3, "digest": 2}


class NtfyNotifier(Notifier):
    """One topic on one ntfy server; no buttons, no edits, no board."""

    def __init__(self, project: Any, cfg: Ntfy) -> None:
        self.project = project
        self.cfg = cfg
        self.token = Path(cfg.token_file).expanduser().read_text().strip() if cfg.token_file else ""

    def publish(self, title: str, text: str, priority: int) -> bool:
        """POST one message as JSON to the server root; True on a 2xx answer."""
        body = {"topic": self.cfg.topic, "title": f"{self.project.project}: {title}", "message": text,
                "priority": priority}
        req = urllib.request.Request(self.cfg.url.rstrip("/") + "/", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300
        except (OSError, ValueError) as e:
            log.warning("ntfy: %s", e)
            return False

    def send(self, kind: str, run_id: str, text: str, buttons: list[Button] | None = None,
             cmd: str | None = None) -> str | None:
        """Publish one alert: the first line of `text` is the title; a button becomes a line with its command."""
        title, _, rest = text.partition("\n")
        self.publish(title, plain(rest or title, buttons, cmd), PRIORITY.get(kind, 4))
        return None

    def post(self, title: str, html: str, silent: bool = False) -> bool:
        return self.publish(title, untag(html), 1 if silent else PRIORITY.get(title, 3))

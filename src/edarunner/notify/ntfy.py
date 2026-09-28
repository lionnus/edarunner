"""The ntfy channel: one push message per alert over HTTP, with a priority by alert kind and copy buttons."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from edarunner.model import Ntfy
from edarunner.notify import Button, Notifier, alerts, button_cmds, untag

log = logging.getLogger(__name__)

# ntfy priorities: 5 urgent, 4 high, 3 default, 2 low, 1 min. An alert kind not listed is high.
PRIORITY = {"dead": 5, "failed": 5, "killed": 5, "host_full": 5, "watch": 5, "superseded": 3, "note": 3, "done": 3,
            "digest": 2, "board": 2, "metrics": 2}
MAX_ACTIONS = 3  # the ntfy limit per message


class NtfyNotifier(Notifier):
    """One topic on one ntfy server; copy buttons, no edits, the board on request only."""

    def __init__(self, project: Any, cfg: Ntfy) -> None:
        self.project = project
        self.cfg = cfg
        self.token = Path(cfg.token_file).expanduser().read_text().strip() if cfg.token_file else ""

    def publish(self, title: str, text: str, priority: int, actions: list[dict] | None = None) -> bool:
        """POST one message as JSON to the server root; True on a 2xx answer.

        A server that refuses the actions with a 400 gets the message again without them."""
        body = {"topic": self.cfg.topic, "title": title, "message": text,
                "priority": priority, **({"actions": actions} if actions else {})}
        req = urllib.request.Request(self.cfg.url.rstrip("/") + "/", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            if e.code == 400 and actions:
                return self.publish(title, text, priority)
            log.warning("ntfy: %s", e)
            return False
        except (OSError, ValueError) as e:
            log.warning("ntfy: %s", e)
            return False

    def send(self, alert: alerts.Alert) -> str | None:
        """Publish one alert: the first line is the title; a button becomes a command line and a copy action."""
        self.publish(alerts.subject(self.project.project, alert), alerts.text(alert),
                     PRIORITY.get(alert.kind, 4), copy_actions(alert.buttons))
        return None

    def post(self, title: str, html: str, silent: bool = False) -> bool:
        return self.publish(f"{self.project.project}: {title}", untag(html), 1 if silent else PRIORITY.get(title, 3))


def copy_actions(buttons: list[Button] | None) -> list[dict]:
    """The alert buttons as ntfy `copy` actions: a tap copies the command of the button."""
    return [{"action": "copy", "label": label, "value": c} for label, c in button_cmds(buttons)][:MAX_ACTIONS]

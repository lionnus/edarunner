"""The notifier interface.

The watcher calls `make_notifiers` once and then `send`, `edit` and `board`
on every channel. The channels never import `cli` or `watch` at run time;
they get their verbs through the `cli.Actions` object the CLI hands in.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from edarunner.ledger import Ledger
from edarunner.model import Project, Site

if TYPE_CHECKING:
    from edarunner.cli import Actions

log = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, callback_data)


class Notifier:
    """One channel. Every method is a no-op here; a channel overrides them."""

    def start(self) -> None:
        """Start a background thread when the channel has one."""

    def stop(self) -> None:
        """Stop that thread."""

    def send(self, kind: str, run_id: str, text: str, buttons: list[Button] | None = None) -> str | None:
        """Send one alert of `kind` for `run_id`; return its message id."""
        return None

    def edit(self, msg_id: str, text: str) -> None:
        """Rewrite a sent message in place."""

    def board(self, text: str) -> None:
        """Rewrite the one pinned board message in place."""


def alert_buttons(handle: str) -> list[Button]:
    """The two buttons of an alert: keep 12 h and ack."""
    return [("keep 12h", f"keep12:{handle}"), ("ack", f"ack:{handle}")]


def make_notifiers(site: Site, project: Project, ledger: Ledger, actions: Actions) -> list[Notifier]:
    """Build every configured channel. A channel without its secret is skipped."""
    out: list[Notifier] = []
    tg = site.telegram
    if tg is not None:
        token_file = os.path.expanduser(str(tg.token_file))
        if not os.path.isfile(token_file):
            log.info("telegram: no token file at %s, bot off", token_file)
        elif os.stat(token_file).st_mode & 0o077:
            log.warning("telegram: %s must be mode 600, bot off", token_file)
        else:
            from edarunner.notify.telegram import TelegramBot

            out.append(TelegramBot(site, project, ledger, actions, token_file))
    return out

"""The notifier interface.

The watcher calls `make_notifiers` once and then `send`, `board` and `post`
on every channel: Telegram, ntfy and mail. docs/notify.md lists what each
channel gets. The channels never import `cli` or `watch` at run time; they
get their commands through the `cli.Actions` object the CLI hands in.
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import TYPE_CHECKING

from edarunner.db import Database
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

    def send(self, kind: str, run_id: str, text: str, buttons: list[Button] | None = None,
             cmd: str | None = None) -> str | None:
        """Send one alert of `kind` for `run_id`: a title line, the reason, and the command to run next; return its message id."""
        return None

    def edit(self, msg_id: str, text: str) -> None:
        """Rewrite a sent message in place."""

    def board(self, text: str) -> None:
        """Rewrite the one pinned board message in place; `text` is Telegram HTML."""

    def post(self, title: str, html: str, silent: bool = False) -> bool:
        """Send one message with the project and `title` in its first line; True when it was sent."""
        return False


def alert_buttons(handle: str) -> list[Button]:
    """The three buttons of an alert: keep 12 h, ack, and stop after the running task."""
    return [("keep 12h", f"keep12:{handle}"), ("ack", f"ack:{handle}"), ("stop", f"stop:{handle}")]


# The shell command of each alert button, for a channel without buttons.
BUTTON_CMDS = {"keep12": "edr keep {} --hours 12", "ack": "edr keep {} --ack", "stop": "edr stop {} --after-task"}


def button_cmds(buttons: list[Button] | None) -> list[tuple[str, str]]:
    """(label, shell command) of each alert button, for a channel without callback buttons."""
    out = []
    for label, data in buttons or []:
        action, _, handle = data.partition(":")
        if action in BUTTON_CMDS:
            out.append((label, BUTTON_CMDS[action].format(handle)))
    return out


def plain(text: str, buttons: list[Button] | None = None, cmd: str | None = None) -> str:
    """An alert body as plain text: `text`, the command to run next, and one line per button."""
    return "\n".join([text, *([cmd] if cmd else []), *(f"{label}: {c}" for label, c in button_cmds(buttons))])


def untag(text: str) -> str:
    """Telegram HTML as plain text."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _private(path: object, channel: str) -> str | None:
    """The expanded path of a secret file, or None when it is absent or readable by others."""
    file = os.path.expanduser(str(path))
    if not os.path.isfile(file):
        log.info("%s: no secret file at %s, channel off", channel, file)
    elif os.stat(file).st_mode & 0o077:
        log.warning("%s: %s must be mode 600, channel off", channel, file)
    else:
        return file
    return None


def make_notifiers(site: Site, project: Project, db: Database, actions: Actions) -> list[Notifier]:
    """Build every configured channel. A channel without its secret is skipped."""
    out: list[Notifier] = []
    tg = site.telegram
    if tg is not None and (token_file := _private(tg.token_file, "telegram")):
        from edarunner.notify.telegram import TelegramBot

        out.append(TelegramBot(site, project, db, actions, token_file))
    nt = getattr(site, "ntfy", None)
    if nt is not None and (nt.token_file is None or _private(nt.token_file, "ntfy")):
        from edarunner.notify.ntfy import NtfyNotifier

        out.append(NtfyNotifier(project, nt))
    mail = getattr(site, "mail", None)
    if mail is not None and (mail.password_file is None or _private(mail.password_file, "mail")):
        from edarunner.notify.mail import MailNotifier

        out.append(MailNotifier(project, mail))
    return out

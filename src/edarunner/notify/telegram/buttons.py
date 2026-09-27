"""The inline buttons of an alert: their markup, the action of a press, and the confirmation of a stop.

A destructive button asks first: the alert shows `Stop <handle>?` with two buttons, and only
the second tap acts. The time of that question is the edit date of the message, so a
confirmation survives a restart of the watcher and expires on its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from edarunner.notify import Button, alert_buttons
from edarunner.notify.telegram.commands import HANDLE

if TYPE_CHECKING:
    from edarunner.cli import Actions

CONFIRM_S = 600  # a question older than this restores the buttons and acts on nothing


def markup(buttons: list[Button]) -> dict:
    """An inline keyboard of one row."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}


@dataclass
class Press:
    """The outcome of one press: the answer on the phone, and the new text and buttons of the alert."""

    answer: str
    text: str
    markup: dict | None


class Buttons:
    """The presses on the alerts of one chat."""

    def __init__(self, actions: Actions, event: Callable[[str, str], None]) -> None:
        self.actions = actions
        self.event = event

    def press(self, q: dict) -> Press:
        """Act on one callback query; a stop asks first and acts on the second tap."""
        verb, _, handle = q.get("data", "").partition(":")
        m = q.get("message") or {}
        text, keys = m.get("text", ""), m.get("reply_markup")
        question = f"Stop {handle}?"
        base = text.rpartition("\n")[0] if text.endswith("\n" + question) else text
        alert = markup(alert_buttons(handle))
        if verb == "stop" and HANDLE.match(handle):
            return Press("tap Yes to stop", base + "\n" + question,
                         markup([("Yes, stop", f"stopyes:{handle}"), ("No", f"stopno:{handle}")]))
        if verb == "stopno":
            return Press("not stopped", base, alert)
        if verb == "stopyes" and time.time() - m.get("edit_date", 0) > CONFIRM_S:
            return Press("the question expired; press stop again", base, alert)
        try:
            if not HANDLE.match(handle):
                note = "unknown button"
            elif verb == "keep12":
                note = self.actions.keep(handle, 12, "telegram") or f"kept {handle} for 12 h"
            elif verb == "ack":
                note = self.actions.ack(handle, "telegram") or f"acked {handle}"
            elif verb == "stopyes":
                note = self.actions.stop_after_task(handle, "telegram", "stopped from a telegram button")
                keys = alert
            else:
                note = "unknown button"
        except Exception as e:  # a refused handle is an answer, not a crash
            note = f"error: {e}"
        if note == "unknown button" or note.startswith("error:"):
            self.event("refused", f"button {q.get('data')}: {note}")
        return Press(note, base + "\n" + note, keys)

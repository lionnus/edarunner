"""The inline buttons of an alert: their markup, the action of a press, and the question before a stop, a delete, a
continue or a kill.

The `callback_data` of a button is `<action>:<project>`, and the run is the one of the alert: the
project's `telegram` store maps the message id of each alert to its run. The supervisor sends the
orphan alerts, so the store of the bot maps the message id of each to its key
`orphan:<host>:<pid>:<owner>`, and Check and End it take their target from there, never from the
text of the message. A button that stops, deletes, kills or starts work asks first: the alert shows
the question with two buttons, and only the second tap acts. The question, what it showed and the
buttons it replaced are kept in the store of the bot, so they survive a restart and expire after
CONFIRM_S.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from edarunner.notify import Button

if TYPE_CHECKING:
    from edarunner.cli import Router

CONFIRM_S = 600  # a question older than this restores the buttons and acts on nothing
# The question of each button that asks first, and the label of its yes.
ASK = {"stop": ("Stop {handle} after its current task?", "Yes, stop"),
       "free": ("Remove the prune targets of the finished runs on {host} whose trees have no stage left?", "Yes, remove"),
       "continue": ("Run the stages left on the tree of {handle} on {host}?", "Yes, continue"),
       "retire": ("Retire {handle}? This removes its tree {run_root} on {host}.", "Yes, retire"),
       "end": ("Send SIGTERM to this process?\nhost: {host}\npid: {pid}\nname: {comm}\nuser: {user}\nstarted: {lstart}\n"
               "cwd: {cwd}\ncommand: {args}\nowner run: {owner_handle} ({state})\nthe same as: {line}", "Yes, end it")}
KEEP = {"keep6": 6, "keep12": 12, "keep24": 24}
ORPHAN = ("check", "end")  # the buttons of an orphan alert


def markup(buttons: list[Button]) -> dict:
    """An inline keyboard of one row."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d, *_ in buttons]]}


@dataclass
class Press:
    """The outcome of one press: the answer on the phone, the new text and buttons of the alert, and the command whose
    reply the bot sends."""

    answer: str
    text: str
    markup: dict | None
    command: list[str] | None = None


class Buttons:
    """The presses on the alerts of one chat, for every project."""

    def __init__(self, router: Router, store: Any, event: Callable[[str, str], None]) -> None:
        self.router = router
        self.store = store
        self.event = event

    def press(self, q: dict) -> Press:
        """Act on one callback query; Stop, Free space, Continue, Retire and End it ask first and act on the second
        tap. End it acts only while the process shows the start time and `EDR_RUN_ID` of its question."""
        action, _, project = q.get("data", "").partition(":")
        m = q.get("message") or {}
        mid, text, keys = str(m.get("message_id")), m.get("text", ""), m.get("reply_markup")
        asked = self.store.get_store("asked", {})
        pending = asked.pop(mid, None)
        if pending:
            self.store.set_store("asked", asked)
            text, keys = pending["text"], pending["markup"]
        base = action.removesuffix("yes").removesuffix("no")
        if base in ORPHAN:
            key = (self.store.get_store("telegram", {}).get("replies", {}).get(mid) or [""])[0]
            run = key if key.startswith("orphan:") and project in self.router.names() else None
        else:
            run = self.router.run_of(project, int(mid)) if project in self.router.names() and mid.isdigit() else None
        if run is None:
            return self._refused(q, "this alert is older than 7 days, or of no registered project; use a command",
                                 text, keys)
        if action in ASK:
            question, yes = ASK[action]
            try:
                with self.router.actions(project) as act:
                    info = act.orphan(run) if action == "end" else act.run_info(run)
            except Exception as e:  # a process that is gone is an answer, not a crash
                return self._refused(q, f"error: {e}", text, keys)
            asked[mid] = {"text": text, "markup": keys, "ts": time.time(), "info": info}
            self.store.set_store("asked", asked)
            return Press("tap the first button to go on", text + "\n" + question.format(**info),
                         markup([(yes, f"{action}yes:{project}", ""), ("No", f"{action}no:{project}", "")]))
        if action.endswith("no") and base in ASK:
            return Press("nothing done", text, keys)
        if action.endswith("yes") and base in ASK and (not pending or time.time() - pending["ts"] > CONFIRM_S):
            return Press("the question expired; press the button again", text, keys)
        if action == "status":
            return Press("the status follows", text, keys, ["status", f"{project}/{run}"])
        try:
            with self.router.actions(project) as act:
                if action in KEEP:
                    note = act.keep(run, KEEP[action], "telegram")
                elif action == "stopyes":
                    note = act.stop_after_task(run, "telegram", "stopped from a telegram button")
                elif action == "freeyes":
                    note = act.free_space(run, "telegram")
                elif action == "continueyes":
                    note = act.continue_run(run, "telegram")
                elif action == "retireyes":
                    note = act.retire(run, "telegram")
                elif action == "check":
                    note = act.check_orphan(run)
                elif action == "endyes":
                    note = act.end_orphan(run, pending["info"], "telegram")
                else:
                    return self._refused(q, "unknown button", text, keys)
        except Exception as e:  # a refused run is an answer, not a crash
            return self._refused(q, f"error: {e}", text, keys)
        return Press(note.splitlines()[0], text + "\n" + note, keys)

    def _refused(self, q: dict, note: str, text: str, keys: dict | None) -> Press:
        self.event("refused", f"button {q.get('data')}: {note}")
        return Press(note, text + "\n" + note, keys)

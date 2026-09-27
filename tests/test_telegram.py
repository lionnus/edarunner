"""The Telegram bot against a recorded `api`, on the local demo project."""

from __future__ import annotations

import io
import logging
import re
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers_telegram import CHAT, USER, FakeActions, FakeApi, FakeDatabase, make_project, make_site

from edarunner import board
from edarunner.model import BotCommand
from edarunner.notify import alert_buttons, make_notifiers
from edarunner.notify.telegram import TelegramBot
from edarunner.notify.telegram import api as tgapi
from edarunner.notify.telegram import bot as tgbot
from edarunner.notify.telegram import custom as tgcustom
from edarunner.notify.telegram import format as fmt
from edarunner.notify.telegram.api import BotApi
from edarunner.notify.telegram.format import LIMIT, fit, pre


@pytest.fixture
def bot(tmp_path, monkeypatch) -> TelegramBot:
    site = make_site(tmp_path)
    bots = make_notifiers(site, make_project(tmp_path), FakeDatabase(), FakeActions())
    assert len(bots) == 1 and isinstance(bots[0], TelegramBot)
    monkeypatch.setattr(bots[0], "api", FakeApi())
    return bots[0]


def msg(text: str, chat: int = CHAT, user: int = USER) -> dict:
    return {"update_id": 1, "message": {"message_id": 100, "chat": {"id": chat}, "from": {"id": user}, "text": text}}


def last_reply(bot: TelegramBot) -> str:
    """The reply under its bold first line, which names the project."""
    head, _, body = bot.api.of("sendMessage")[-1]["text"].partition("\n")
    assert head.startswith("<b>demo: ") and head.endswith("</b>")
    return body


def test_make_notifiers_needs_a_private_token(tmp_path):
    site = make_site(tmp_path, mode=0o644)
    assert make_notifiers(site, make_project(tmp_path), FakeDatabase(), FakeActions()) == []
    site.telegram.token_file.unlink()
    assert make_notifiers(site, make_project(tmp_path), FakeDatabase(), FakeActions()) == []
    assert make_notifiers(SimpleNamespace(telegram=None), None, None, None) == []


@pytest.mark.parametrize("text,call", [
    ("/status", ("status_text", (None,), {})),
    ("/status@edr_bot", ("status_text", (None,), {})),
    ("/status a@demo", ("status_text", ("a@demo",), {})),
    ("/events 5", ("events_text", (5,), {})),
    ("/events 500", ("events_text", (30,), {})),
    ("/events", ("events_text", (8,), {})),
    ("/hosts", ("hosts_text", (), {})),
    ("/tools", ("tools_text", (), {})),
    ("/keep a@demo 6", ("keep", ("a@demo", 6, "telegram"), {})),
    ("/keep a@demo", ("keep", ("a@demo", 12, "telegram"), {})),
    ("/ack #3", ("ack", ("#3", "telegram"), {})),
    ("/stop a@demo disk full", ("stop_after_task", ("a@demo", "telegram", "disk full"), {})),
    ("/stop a@demo", ("stop_after_task", ("a@demo", "telegram", "stopped from telegram"), {})),
    ("/compare a@demo b_nodw@demo", ("compare_text", (["a@demo", "b_nodw@demo"],), {})),
    ("/metric power_w --design HEAD", ("metric_text", ("power_w", "HEAD"), {})),
    ("/metric power_w", ("metric_text", ("power_w", None), {})),
])
def test_builtin_dispatch(bot, text, call):
    bot.handle_update(msg(text))
    assert call in bot.actions.calls
    out = call[0] + " ok"
    assert last_reply(bot) == (pre(out) if call[0] in ("compare_text", "metric_text") else out)



def test_a_removed_command_names_its_replacement(bot):
    bot.handle_update(msg("/lic"))
    assert bot.actions.calls == [] and last_reply(bot) == "/lic was removed; use /tools"

def test_bad_handle_is_an_answer(bot):
    bot.handle_update(msg("/keep 'a;rm' 3"))
    assert bot.actions.calls == []
    assert "error" in last_reply(bot)


def test_help_groups_builtins_and_custom(bot):
    bot.handle_update(msg("/nothing"))
    assert bot.api.of("sendMessage")[-1]["text"].startswith("<b>demo: help</b>\n<b>Look</b>\n/status [handle]: ")
    text = last_reply(bot)
    assert "<pre>" not in text and "/keep &lt;handle&gt; [hours]: add hours, default 12" in text
    assert "<b>Custom</b>\n/echo &lt;dir&gt;: echo a dir" in text
    assert [ln for ln in text.splitlines() if ln.startswith("<b>")] == ["<b>Look</b>", "<b>Files</b>", "<b>Act on a run</b>", "<b>Compare</b>", "<b>Help</b>", "<b>Custom</b>"]


def test_custom_good_argument_runs_argv(bot):
    bot.handle_update(msg("/echo backend"))
    assert last_reply(bot) == pre("demo/backend")
    assert bot.db.events[-1]["kind"] == "command"
    assert bot.db.events[-1]["actor"] == "telegram"


def test_custom_bad_argument_is_refused(bot):
    bot.handle_update(msg("/echo backend;rm"))
    assert last_reply(bot).startswith("<pre>refused: dir must match")
    assert [e["kind"] for e in bot.db.events] == ["refused"]


def test_custom_last_argument_takes_the_rest(bot):
    bot.handle_update(msg("/ask how many runs are live?"))
    assert last_reply(bot) == pre("how many runs are live?")
    bot.handle_update(msg("/ask"))
    assert last_reply(bot) == pre("usage: /ask <question>")


def test_custom_without_args_refuses_extra_words(bot):
    bot.handle_update(msg("/skip extra"))
    assert last_reply(bot).startswith("<pre>usage: /skip")
    assert bot.db.events == []


def test_custom_skip_if_renders_placeholders(bot):
    bot.handle_update(msg("/same demo"))
    assert last_reply(bot) == pre("skipped demo")
    bot.handle_update(msg("/same other"))
    assert last_reply(bot) == pre("ran other")


def test_keep_refuses_a_unicode_digit(bot):
    bot.handle_update(msg("/keep x \u00b2"))
    assert bot.actions.calls == []
    assert last_reply(bot).startswith("error: ")
    assert [e["kind"] for e in bot.db.events] == ["refused"]


def test_custom_dry_run_skip_detach_timeout(bot, monkeypatch):
    monkeypatch.setattr(tgcustom, "DETACH_WATCH_S", 0.3)
    bot.handle_update(msg("/dry x"))
    assert "would run in" in last_reply(bot) and "rm -rf x" in last_reply(bot)
    bot.handle_update(msg("/skip"))
    assert last_reply(bot) == pre("already demo")
    bot.handle_update(msg("/bg"))
    assert "started demo (pid " in last_reply(bot) and (Path(bot.project.data) / "telegram-bg.log").exists()
    monkeypatch.setattr(tgcustom, "DETACH_WATCH_S", 5.0)
    bot.handle_update(msg("/dies"))
    assert last_reply(bot) == pre("ended with rc 3: Workspace not trusted")
    assert bot.api.of("setMessageReaction")[-1]["reaction"][0]["emoji"] == "👎"
    bot.handle_update(msg("/dies"))
    assert last_reply(bot) == pre("ended with rc 3: Workspace not trusted")
    assert (Path(bot.project.data) / "telegram-dies.log").read_text().count("loading") == 2
    bot.handle_update(msg("/slow"))
    assert "timed out after 1 s" in last_reply(bot)


def test_allowlist_is_silent_with_one_event(bot):
    bot.handle_update(msg("/status", chat=7))
    bot.handle_update(msg("/status", chat=7))
    assert bot.api.calls == [] and bot.actions.calls == []
    assert [e["kind"] for e in bot.db.events] == ["rejected"]
    assert "chat 7" in bot.db.events[0]["text"]


def test_chat_id_zero_prints_the_chat_id(tmp_path, monkeypatch, capsys):
    site = make_site(tmp_path, chat_id=0)
    b = TelegramBot(site, make_project(tmp_path), FakeDatabase(), FakeActions(), str(site.telegram.token_file))
    monkeypatch.setattr(b, "api", FakeApi())
    b.handle_update(msg("/status", chat=99))
    assert "chat_id = 99" in capsys.readouterr().err
    assert b.api.calls == []


def callback(data: str, chat: int = CHAT, user: int = USER) -> dict:
    markup = {"inline_keyboard": [[{"text": "ack", "callback_data": "ack:a@demo"}]]}
    # A press in a group comes from a user id; without user_id only the chat that holds the button is checked.
    return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": user}, "data": data,
                                               "message": {"message_id": 5, "chat": {"id": chat}, "text": "hung a@demo",
                                                           "entities": [{"offset": 0, "length": 4, "type": "bold"}],
                                                           "reply_markup": markup}}}


def test_callback_buttons(bot):
    bot.handle_update(callback("keep12:a@demo"))
    assert ("keep", ("a@demo", 12, "telegram"), {}) in bot.actions.calls
    bot.handle_update(callback("ack:a@demo"))
    assert ("ack", ("a@demo", "telegram"), {}) in bot.actions.calls
    answers = bot.api.of("answerCallbackQuery")
    assert [a["callback_query_id"] for a in answers] == ["cb1", "cb1"]
    edits = bot.api.of("editMessageText")
    assert edits[-1]["message_id"] == 5 and edits[-1]["text"] == "hung a@demo\nack ok"
    assert edits[-1]["entities"] == [{"offset": 0, "length": 4, "type": "bold"}]  # the bold title stays
    assert edits[-1]["reply_markup"]["inline_keyboard"]  # the buttons stay
    assert bot.db.events == []  # the action records its own event
    bot.handle_update(callback("retire:a@demo"))
    assert bot.api.of("answerCallbackQuery")[-1]["text"] == "unknown button"
    assert [e["kind"] for e in bot.db.events] == ["refused"]
    assert len(bot.actions.calls) == 2
    bot.handle_update(callback("ack:a@demo", chat=7))
    assert len(bot.actions.calls) == 2


def test_press_by_another_user_needs_no_user_id(bot, caplog):
    bot._stop.set()  # the poll thread ends at once
    with caplog.at_level(logging.WARNING):
        bot.start()
    bot.stop()
    assert "chat 42 is the only gate" in caplog.text
    bot.handle_update(callback("ack:a@demo", user=999))
    assert ("ack", ("a@demo", "telegram"), {}) in bot.actions.calls
    bot.handle_update(msg("/status", user=999))
    assert last_reply(bot) == "status_text ok"


def test_a_project_without_poll_sends_alerts_only(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    project = make_project(tmp_path)
    project.telegram_poll = False
    b = TelegramBot(site, project, FakeDatabase(), FakeActions(), str(site.telegram.token_file))
    monkeypatch.setattr(b, "api", FakeApi())
    b.start()
    b.stop()
    assert b._thread is None and b.api.of("setMyCommands") == []
    b.send("dead", "a@demo", "no heartbeat", alert_buttons("a@demo"))
    assert len(b.api.of("sendMessage")) == 1 and b.api.of("sendMessage")[0]["reply_markup"] is None


def test_user_id_gates_the_allowed_chat(tmp_path, monkeypatch, caplog):
    site = make_site(tmp_path, user_id=777)
    b = TelegramBot(site, make_project(tmp_path), FakeDatabase(), FakeActions(), str(site.telegram.token_file))
    monkeypatch.setattr(b, "api", FakeApi())
    b._stop.set()
    with caplog.at_level(logging.WARNING):
        b.start()
    b.stop()
    assert "only gate" not in caplog.text
    b.handle_update(msg("/status", user=USER))
    b.handle_update(callback("ack:a@demo", user=USER))
    b.handle_update(msg("/stop a@demo", user=USER))
    assert b.api.of("sendMessage") == [] and b.api.of("answerCallbackQuery") == [] and b.actions.calls == []
    assert [e["kind"] for e in b.db.events] == ["rejected"] and "user 12345 in chat 42" in b.db.events[0]["text"]
    b.handle_update(msg("/status", user=777))
    b.handle_update(callback("ack:a@demo", user=777))
    assert [c[0] for c in b.actions.calls] == ["status_text", "ack"]
    b.handle_update(msg("/status", chat=7, user=777))
    assert len(b.actions.calls) == 2


def test_alert_send_edits_a_repeat(bot):
    mid = bot.send("hung", "run1", "hung a@demo\nno progress <3 h", alert_buttons("a@demo"), "edr stop a@demo --why hung")
    sent = bot.api.of("sendMessage")[-1]
    assert mid == "1" and sent["disable_notification"] is False
    assert sent["text"] == "🔴 <b>demo: hung</b> <code>a@demo</code>\nno progress &lt;3 h\n<code>edr stop a@demo --why hung</code>"
    assert [b["callback_data"] for b in sent["reply_markup"]["inline_keyboard"][0]] == ["keep12:a@demo", "ack:a@demo", "stop:a@demo"]
    assert bot.send("hung", "run1", "no progress for 3 h") == "1"
    assert bot.api.of("editMessageText")[-1]["message_id"] == 1
    assert bot.send("hung", "run2", "x") == "2"


def test_board_is_created_once_then_edited(bot, tmp_path):
    bot.board("board v1")
    sent = bot.api.of("sendMessage")
    assert len(sent) == 1 and sent[0]["disable_notification"] is True
    assert bot.api.of("pinChatMessage")[0]["message_id"] == 1
    assert bot.db.store["telegram"]["board"] == 1 and not (tmp_path / "data").exists()
    bot.board("board v2")
    assert len(bot.api.of("sendMessage")) == 1
    edit = bot.api.of("editMessageText")[-1]
    assert edit["message_id"] == 1 and edit["reply_markup"] is None
    assert re.fullmatch(r"<b>demo: board \d\d:\d\d</b>\nboard v2", edit["text"])
    # A new bot on the same db edits the same message.
    again = TelegramBot(bot.site, bot.project, bot.db, bot.actions, str(bot.tg.token_file))
    again.api = FakeApi()
    again.board("board v3")
    assert again.api.of("sendMessage") == [] and again.api.of("editMessageText")[-1]["message_id"] == 1
    # /pin unpins the old message and pins a new one.
    bot.handle_update(msg("/pin"))
    assert bot.api.of("unpinChatMessage")[-1]["message_id"] == 1
    assert bot.api.of("pinChatMessage")[-1]["message_id"] == 2


def test_api_obeys_429_retry_after(monkeypatch):
    b = BotApi("123:ABC")
    slept: list[int] = []
    monkeypatch.setattr(tgapi.time, "sleep", slept.append)
    answers = [
        urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b'{"ok":false,"parameters":{"retry_after":3}}')),
        io.BytesIO(b'{"ok":true,"result":{"message_id":9}}'),
    ]

    def fake_urlopen(req, timeout):
        assert req.full_url.startswith("https://api.telegram.org/bot123:ABC/") and timeout == 15
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(tgapi, "urlopen", fake_urlopen)
    assert b.call("sendMessage", {"chat_id": CHAT, "text": "x"}) == {"message_id": 9}
    assert slept == [3]
    answers.append(urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"description":"Bad Request: message is not modified"}')))
    assert b.call("editMessageText", {"chat_id": CHAT}) == {}
    answers.append(urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"description":"Bad Request: chat not found"}')))
    with pytest.raises(tgapi.ApiError, match="chat not found"):
        b.call("sendMessage", {"chat_id": CHAT})


def test_api_retries_idempotent_methods_only(monkeypatch):
    b = BotApi("123:ABC")
    slept: list[int] = []
    monkeypatch.setattr(tgapi.time, "sleep", slept.append)
    tries: list[str] = []

    def fake_urlopen(req, timeout):
        tries.append(req.full_url.rsplit("/", 1)[1])
        raise TimeoutError("timed out")

    monkeypatch.setattr(tgapi, "urlopen", fake_urlopen)
    with pytest.raises(tgapi.ApiError, match="sendMessage: timed out"):
        b.call("sendMessage", {"chat_id": CHAT, "text": "x"})
    assert tries == ["sendMessage"] and slept == []
    for method in ("getUpdates", "editMessageText", "answerCallbackQuery"):
        tries.clear()
        with pytest.raises(tgapi.ApiError, match=method):
            b.call(method, {"chat_id": CHAT})
        assert tries == [method] * 3
    assert slept == [5, 10, 15] * 3


def test_builtin_commands_land_in_events(bot):
    bot.handle_update(msg("/pin"))
    bot.handle_update(msg("/keep 'a;rm' 3"))
    bot.handle_update(msg("/ack a@demo"))  # the action records its own event
    assert [(e["kind"], e["text"][:6]) for e in bot.db.events] == [("command", "/pin"), ("refused", "/keep ")]


def test_poll_survives_a_bad_response(bot, monkeypatch):
    calls: list[str] = []

    def api(method, params, files=None):
        calls.append(method)
        if len(calls) == 1:
            raise ValueError("not json")
        bot._stop.set()
        return []

    monkeypatch.setattr(bot.api, "call", api)
    monkeypatch.setattr(tgapi.time, "sleep", lambda s: None)
    bot._poll()
    assert calls == ["getUpdates", "getUpdates"]


def test_api_turns_a_bad_body_into_apierror(monkeypatch):
    b = BotApi("123:ABC")
    monkeypatch.setattr(tgapi.time, "sleep", lambda s: None)
    html = b"<html>502 Bad Gateway</html>"
    answers = [urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(html))] + [io.BytesIO(html) for _ in range(3)]

    def fake_urlopen(req, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(tgapi, "urlopen", fake_urlopen)
    with pytest.raises(tgapi.ApiError, match="502"):
        b.call("getUpdates", {"offset": None})
    with pytest.raises(tgapi.ApiError, match="Expecting value"):
        b.call("getUpdates", {"offset": None})
    assert answers == []


def test_custom_output_keeps_a_non_utf8_byte(bot):
    bot.tg.commands["latin"] = BotCommand("latin", "latin", ["printf", "caf\\xe9\\n"])
    bot.handle_update(msg("/latin"))
    assert last_reply(bot) == pre("caf\ufffd")


def test_pre_escapes_and_cuts():
    assert pre("a<b>&") == "<pre>a&lt;b&gt;&amp;</pre>"
    assert len(pre("x" * 5000)) == 4000 + len("<pre></pre>")


def test_every_run_state_has_a_mark():
    states = set(board.STYLE) | set(board.RANK) | {"queued", "orphan", "retired", "imported"}
    states |= {t.lower() for t in board.TERMINAL}
    assert states <= set(fmt.MARK) and len(set(fmt.MARK.values())) == 7


def test_a_long_reply_is_cut_at_a_line(bot):
    text = fit("\n".join(f"<i>line {n}</i>" for n in range(1000)))
    assert len(text) <= LIMIT + 2 and text.endswith("</i>\n…")
    bot.actions.status_text = lambda h=None: "\n".join(["<code>x</code>"] * 1000)
    bot.handle_update(msg("/status"))
    assert len(bot.api.of("sendMessage")[-1]["text"]) <= LIMIT + 2


def test_an_alert_without_a_state_keeps_its_title(bot):
    bot.send("watch", "", "watch stale\nno watch.json")
    assert bot.api.of("sendMessage")[-1]["text"] == "<b>demo: watch stale</b>\nno watch.json"


def in_topic(update: dict, thread: int) -> dict:
    """`update` with its message in forum thread `thread`."""
    m = update.get("message") or update["callback_query"]["message"]
    m.update(message_thread_id=thread, is_topic_message=True)
    return update


def test_a_topic_routes_every_message_and_ignores_other_threads(bot, capsys):
    bot.topic = 17
    bot.handle_update(in_topic(msg("/status"), 99))
    bot.handle_update(in_topic(callback("ack:a@demo"), 99))
    bot.handle_update(msg("/status"))
    assert bot.api.calls == [] and bot.actions.calls == [] and bot.db.events == []
    bot.handle_update(in_topic(msg("/status"), 17))
    assert bot.api.of("sendMessage")[-1]["message_thread_id"] == 17
    bot.send("dead", "run1", "dead a@demo\nno heartbeat", alert_buttons("a@demo"))
    bot.board("board v1")
    assert [p["message_thread_id"] for p in bot.api.of("sendMessage")] == [17, 17, 17]
    assert bot.api.of("pinChatMessage")[0]["message_id"] == 3
    assert capsys.readouterr().err == ""


def test_without_a_topic_the_reply_goes_to_the_thread_and_its_id_is_printed(bot, capsys):
    bot.handle_update(in_topic(msg("/status"), 99))
    bot.handle_update(in_topic(msg("/status"), 99))
    assert [p["message_thread_id"] for p in bot.api.of("sendMessage")] == [99, 99]
    assert capsys.readouterr().err.count("set topic_id = 99 in [telegram] of edr.toml") == 1
    bot.handle_update(msg("/status"))
    assert bot.api.of("sendMessage")[-1]["message_thread_id"] is None


def reply_to(text: str, msg_id: int) -> dict:
    """A message that replies to the bot's message `msg_id`."""
    u = msg(text)
    u["message"]["reply_to_message"] = {"message_id": msg_id}
    return u


def test_a_reply_to_an_alert_names_its_run(bot, monkeypatch):
    mid = int(bot.send("hung", "run1", "hung a@demo\nno progress", alert_buttons("a@demo")))
    for text, call in (("/keep 24", ("keep", ("run1", 24, "telegram"), {})),
                       ("/keep run1 6", ("keep", ("run1", 6, "telegram"), {})),
                       ("/ack", ("ack", ("run1", "telegram"), {})),
                       ("/stop disk full", ("stop_after_task", ("run1", "telegram", "disk full"), {})),
                       ("/status", ("status_text", ("run1",), {}))):
        bot.handle_update(reply_to(text, mid))
        assert bot.actions.calls[-1] == call, text
    bot.handle_update(reply_to("/where", mid))
    assert last_reply(bot) == pre("a@demo run1 hostA:/scratch/edr/demo/run1")
    bot.handle_update(msg("/where"))
    assert last_reply(bot) == pre("/where: missing placeholder {handle} in '{handle} {run_id} {host}:{run_root}'")
    bot.handle_update(reply_to("/ack", 999))
    assert last_reply(bot).startswith("error: a handle is")
    monkeypatch.setattr(tgbot.time, "time", lambda: 1e12)
    bot.handle_update(reply_to("/ack", mid))
    assert last_reply(bot).startswith("error: a handle is")
    assert list(bot.db.store["telegram"]["replies"]) == [str(mid)]


def test_post_sends_one_message_and_says_whether_it_went(bot, monkeypatch):
    bot.topic = 17
    assert bot.post("note", "a &amp; b", silent=True) is True
    sent = bot.api.of("sendMessage")[-1]
    assert sent["text"] == "<b>demo: note</b>\na &amp; b"
    assert sent["disable_notification"] is True and sent["message_thread_id"] == 17

    def refuse(method, params, files=None):
        raise tgapi.ApiError("sendMessage: chat not found")

    monkeypatch.setattr(bot.api, "call", refuse)
    assert bot.post("note", "x") is False


def test_files_go_up_as_documents_under_the_limit(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/log a@demo 3"))
    doc = bot.api.of("sendDocument")[-1]
    assert bot.actions.calls[-1] == ("log_tail", ("a@demo", 3), {})
    assert doc["files"] == {"document": ("a@demo.log", b"line\n" * 3)}
    assert doc["caption"] == "<b>demo: log</b>\nthe last 3 lines"
    bot.handle_update(msg("/log a@demo"))
    assert bot.actions.calls[-1] == ("log_tail", ("a@demo", 200), {})
    bot.handle_update(msg("/board"))
    assert last_reply(bot) == "no board files yet; the watcher writes them every cycle"
    for name in ("compare.html", "status.html"):
        (tmp_path / name).write_text(f"<html>{name}</html>")
        bot.actions.board.append(tmp_path / name)
    bot.handle_update(msg("/board"))
    assert [d["files"]["document"][0] for d in bot.api.of("sendDocument")[-2:]] == ["compare.html", "status.html"]
    bot.handle_update(msg("/csv gabc1234"))
    assert bot.api.of("sendDocument")[-1]["files"] == {"document": ("metrics.csv", b"run_id,src\nr1,gabc1234\n")}
    bot.handle_update(msg("/csv a;b"))
    assert last_reply(bot) == "usage: /csv &lt;design&gt;"
    monkeypatch.setattr(tgbot, "MAX_DOCUMENT", 2**20)
    sent = len(bot.api.of("sendDocument"))
    bot.handle_update(msg("/log a@demo 300000"))
    assert len(bot.api.of("sendDocument")) == sent
    assert last_reply(bot) == "a@demo.log: 1.4 MB is over the limit of 1 MB"


def test_send_document_posts_a_multipart_body(monkeypatch):
    seen: list = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        return io.BytesIO(b'{"ok":true,"result":{"message_id":4}}')

    monkeypatch.setattr(tgapi, "urlopen", fake_urlopen)
    assert BotApi("1:A").send_document(CHAT, "a@demo.log", b"x\n", "<b>demo: log</b>", thread_id=17) == 4
    req = seen[0]
    assert req.full_url.endswith("/sendDocument") and req.headers["Content-type"].startswith("multipart/form-data")
    assert b'name="message_thread_id"\r\n\r\n17\r\n' in req.data
    assert b'filename="a@demo.log"\r\nContent-Type: application/octet-stream\r\n\r\nx\n\r\n' in req.data


def press(data: str, text: str, edited: float) -> dict:
    """A press on alert 5 with `text`, last edited at `edited`."""
    u = callback(data)
    u["callback_query"]["message"].update(text=text, edit_date=int(edited))
    return u


def test_the_stop_button_asks_and_acts_on_the_second_tap(bot):
    now = time.time()
    bot.handle_update(press("stop:a@demo", "hung a@demo", now - 3600))
    edit = bot.api.of("editMessageText")[-1]
    assert edit["text"] == "hung a@demo\nStop a@demo?" and bot.actions.calls == []
    assert [b["callback_data"] for b in edit["reply_markup"]["inline_keyboard"][0]] == ["stopyes:a@demo", "stopno:a@demo"]
    assert edit["entities"] == [{"offset": 0, "length": 4, "type": "bold"}]
    bot.handle_update(press("stopno:a@demo", "hung a@demo\nStop a@demo?", now))
    edit = bot.api.of("editMessageText")[-1]
    assert edit["text"] == "hung a@demo" and bot.actions.calls == []
    assert edit["reply_markup"]["inline_keyboard"][0][2]["callback_data"] == "stop:a@demo"
    bot.handle_update(press("stopyes:a@demo", "hung a@demo\nStop a@demo?", now - 601))
    assert bot.api.of("answerCallbackQuery")[-1]["text"] == "the question expired; press stop again"
    assert bot.api.of("editMessageText")[-1]["text"] == "hung a@demo" and bot.actions.calls == []
    bot.handle_update(press("stopyes:a@demo", "hung a@demo\nStop a@demo?", now - 5))
    assert bot.actions.calls == [("stop_after_task", ("a@demo", "telegram", "stopped from a telegram button"), {})]
    edit = bot.api.of("editMessageText")[-1]
    assert edit["text"] == "hung a@demo\nstop_after_task ok" and len(edit["reply_markup"]["inline_keyboard"][0]) == 3


def test_the_reply_keyboard_sends_plain_words(bot):
    bot.handle_update(msg("/start"))
    sent = bot.api.of("sendMessage")[-1]
    assert sent["text"].startswith("<b>demo: help</b>\n<b>Look</b>")
    assert sent["reply_markup"]["keyboard"] == [[{"text": "Status"}, {"text": "Hosts"}],
                                                [{"text": "Events"}, {"text": "Tools"}, {"text": "Digest"}]]
    assert sent["reply_markup"]["is_persistent"] is True
    for word, call in (("Status", "status_text"), ("hosts", "hosts_text"), (" Events ", "events_text"), ("Tools", "tools_text"),
                       ("Digest", "digest_text")):
        bot.handle_update(msg(word))
        assert bot.actions.calls[-1][0] == call
    bot.handle_update(msg("status please"))
    assert bot.actions.calls[-1][0] == "digest_text"
    bot.handle_update(msg("/keyboard off"))
    assert bot.api.of("sendMessage")[-1]["reply_markup"] == {"remove_keyboard": True}
    bot.handle_update(msg("/keyboard"))
    assert last_reply(bot) == "keyboard on" and "keyboard" in bot.api.of("sendMessage")[-1]["reply_markup"]
    assert [e["text"] for e in bot.db.events][:2] == ["/start", "/status"]


def reactions(bot) -> list[str]:
    return [p["reaction"][0]["emoji"] for p in bot.api.of("setMessageReaction")]


def test_a_command_message_gets_a_reaction(bot, monkeypatch):
    bot.handle_update(msg("/status"))
    assert reactions(bot) == ["👍"] and bot.api.of("setMessageReaction")[0]["message_id"] == 100
    bot.handle_update(msg("/hosts"))
    bot.handle_update(msg("/echo backend"))
    assert reactions(bot)[1:] == ["👀", "👍", "👀", "👍"]
    bot.api.calls.clear()
    bot.handle_update(msg("/echo nope"))
    bot.handle_update(msg("/keep 'a;rm'"))
    assert reactions(bot) == ["👀", "👎", "👎"]

    def no_reactions(method, params, files=None):
        if method == "setMessageReaction":
            raise tgapi.ApiError("setMessageReaction: Bad Request: REACTION_INVALID")
        return {"message_id": 1}

    monkeypatch.setattr(bot.api, "call", no_reactions)
    bot.handle_update(msg("/status"))
    assert bot.actions.calls[-1][0] == "status_text"

"""The Telegram bot against a recorded `api`, on the local demo project."""

from __future__ import annotations

import io
import json
import logging
import re
import tomllib
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from edarunner.model import BotCommand, Host, Site, Telegram
from edarunner.notify import alert_buttons, make_notifiers
from edarunner.notify import telegram as tgmod
from edarunner.notify.telegram import TelegramBot, pre

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
CHAT = 42
USER = 12345


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.n = 0

    def __call__(self, method: str, params: dict, files=None) -> dict:
        self.calls.append((method, params))
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n}
        return {}

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):
        def f(*a, **k):
            self.calls.append((name, a, k))
            return f"{name} ok"

        return f


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kv: dict = {}

    def add_event(self, **kw) -> None:
        self.events.append(kw)

    def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    def set_kv(self, key, value) -> None:
        self.kv[key] = json.loads(json.dumps(value))


COMMANDS = {
    "echo": BotCommand("echo", "echo a dir", ["echo", "{project}/{dir}"], args={"dir": "^(backend|paper)$"}),
    "dry": BotCommand("dry", "dry", ["rm", "-rf", "{dir}"], args={"dir": "^\\w+$"}, dry_run=True),
    "skip": BotCommand("skip", "skip", ["false"], skip_if=["true"], skip_reply="already {project}"),
    "bg": BotCommand("bg", "bg", ["sleep", "0"], detach=True, reply="started {project}"),
    "ask": BotCommand("ask", "ask", ["echo", "{question}"], args={"question": "^[\\w ?]{1,40}$"}),
    "slow": BotCommand("slow", "slow", ["sleep", "5"], timeout_s=1),
    "same": BotCommand("same", "same", ["echo", "ran {dir}"], args={"dir": "^\\w+$"},
                       skip_if=["test", "{dir}", "=", "{project}"], skip_reply="skipped {dir}"),
}


def make_site(tmp_path: Path, chat_id: int = CHAT, mode: int = 0o600, user_id: int | None = None) -> Site:
    token = tmp_path / "telegram.token"
    token.write_text("123:ABC\n")
    token.chmod(mode)
    return Site(path=DEMO / "site.toml", scratch=["/tmp/edr-demo"], env={}, ssh_options=[], ssh_timeout_s=20,
                tool_procs="^(sleep)$", hosts={"local": Host("local", 4, 8)},
                telegram=Telegram(token_file=token, chat_id=chat_id, commands=dict(COMMANDS), user_id=user_id))


def make_project(tmp_path: Path) -> SimpleNamespace:
    # A Project needs every stage; the bot reads three fields of it.
    edr = tomllib.loads((DEMO / "edr.toml").read_text())
    return SimpleNamespace(project=edr["project"], root=DEMO, data=tmp_path / "data", telegram_poll=True)


@pytest.fixture
def bot(tmp_path, monkeypatch) -> TelegramBot:
    site = make_site(tmp_path)
    bots = make_notifiers(site, make_project(tmp_path), FakeLedger(), FakeActions())
    assert len(bots) == 1 and isinstance(bots[0], TelegramBot)
    monkeypatch.setattr(bots[0], "api", FakeApi())
    return bots[0]


def msg(text: str, chat: int = CHAT, user: int = USER) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}


def last_reply(bot: TelegramBot) -> str:
    """The reply under its bold first line, which names the project."""
    head, _, body = bot.api.of("sendMessage")[-1]["text"].partition("\n")
    assert head.startswith("<b>demo · ") and head.endswith("</b>")
    return body


def test_make_notifiers_needs_a_private_token(tmp_path):
    site = make_site(tmp_path, mode=0o644)
    assert make_notifiers(site, make_project(tmp_path), FakeLedger(), FakeActions()) == []
    site.telegram.token_file.unlink()
    assert make_notifiers(site, make_project(tmp_path), FakeLedger(), FakeActions()) == []
    assert make_notifiers(SimpleNamespace(telegram=None), None, None, None) == []


@pytest.mark.parametrize("text,call", [
    ("/status", ("status_text", (None,), {})),
    ("/status@edr_bot", ("status_text", (None,), {})),
    ("/status a@demo", ("status_text", ("a@demo",), {})),
    ("/events 5", ("events_text", (5,), {})),
    ("/events 500", ("events_text", (30,), {})),
    ("/events", ("events_text", (8,), {})),
    ("/hosts", ("hosts_text", (), {})),
    ("/lic", ("lic_text", (), {})),
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
    assert last_reply(bot) == pre(call[0] + " ok")


def test_bad_handle_is_an_answer(bot):
    bot.handle_update(msg("/keep 'a;rm' 3"))
    assert bot.actions.calls == []
    assert "error" in last_reply(bot)


def test_help_groups_builtins_and_custom(bot):
    bot.handle_update(msg("/nothing"))
    assert bot.api.of("sendMessage")[-1]["text"].startswith("<b>demo · help</b>\n<b>Look</b>\n/status [handle] · ")
    text = last_reply(bot)
    assert "<pre>" not in text and "/keep &lt;handle&gt; [hours] · add hours, default 12" in text
    assert "<b>Custom</b>\n/echo &lt;dir&gt; · echo a dir" in text
    assert [ln for ln in text.splitlines() if ln.startswith("<b>")] == ["<b>Look</b>", "<b>Act on a run</b>", "<b>Compare</b>", "<b>Custom</b>"]


def test_custom_good_argument_runs_argv(bot):
    bot.handle_update(msg("/echo backend"))
    assert last_reply(bot) == pre("demo/backend")
    assert bot.ledger.events[-1]["kind"] == "command"
    assert bot.ledger.events[-1]["actor"] == "telegram"


def test_custom_bad_argument_is_refused(bot):
    bot.handle_update(msg("/echo backend;rm"))
    assert last_reply(bot).startswith("<pre>refused: dir must match")
    assert [e["kind"] for e in bot.ledger.events] == ["refused"]


def test_custom_last_argument_takes_the_rest(bot):
    bot.handle_update(msg("/ask how many runs are live?"))
    assert last_reply(bot) == pre("how many runs are live?")
    bot.handle_update(msg("/ask"))
    assert last_reply(bot) == pre("usage: /ask <question>")


def test_custom_without_args_refuses_extra_words(bot):
    bot.handle_update(msg("/skip extra"))
    assert last_reply(bot).startswith("<pre>usage: /skip")
    assert bot.ledger.events == []


def test_custom_skip_if_renders_placeholders(bot):
    bot.handle_update(msg("/same demo"))
    assert last_reply(bot) == pre("skipped demo")
    bot.handle_update(msg("/same other"))
    assert last_reply(bot) == pre("ran other")


def test_keep_refuses_a_unicode_digit(bot):
    bot.handle_update(msg("/keep x \u00b2"))
    assert bot.actions.calls == []
    assert last_reply(bot).startswith("<pre>error")
    assert [e["kind"] for e in bot.ledger.events] == ["refused"]


def test_custom_dry_run_skip_detach_timeout(bot):
    bot.handle_update(msg("/dry x"))
    assert "would run in" in last_reply(bot) and "rm -rf x" in last_reply(bot)
    bot.handle_update(msg("/skip"))
    assert last_reply(bot) == pre("already demo")
    bot.handle_update(msg("/bg"))
    assert "started demo (pid " in last_reply(bot) and (Path(bot.project.data) / "telegram-bg.log").exists()
    bot.handle_update(msg("/slow"))
    assert "timed out after 1 s" in last_reply(bot)


def test_allowlist_is_silent_with_one_event(bot):
    bot.handle_update(msg("/status", chat=7))
    bot.handle_update(msg("/status", chat=7))
    assert bot.api.calls == [] and bot.actions.calls == []
    assert [e["kind"] for e in bot.ledger.events] == ["rejected"]
    assert "chat 7" in bot.ledger.events[0]["text"]


def test_chat_id_zero_prints_the_chat_id(tmp_path, monkeypatch, capsys):
    site = make_site(tmp_path, chat_id=0)
    b = TelegramBot(site, make_project(tmp_path), FakeLedger(), FakeActions(), str(site.telegram.token_file))
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
    assert [e["kind"] for e in bot.ledger.events] == ["button", "button"]
    bot.handle_update(callback("retire:a@demo"))
    assert bot.api.of("answerCallbackQuery")[-1]["text"] == "unknown button"
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
    assert last_reply(bot) == pre("status_text ok")


def test_a_project_without_poll_sends_alerts_only(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    project = make_project(tmp_path)
    project.telegram_poll = False
    b = TelegramBot(site, project, FakeLedger(), FakeActions(), str(site.telegram.token_file))
    monkeypatch.setattr(b, "api", FakeApi())
    b.start()
    b.stop()
    assert b._thread is None and b.api.of("setMyCommands") == []
    b.send("dead", "a@demo", "no heartbeat")
    assert len(b.api.of("sendMessage")) == 1


def test_user_id_gates_the_allowed_chat(tmp_path, monkeypatch, caplog):
    site = make_site(tmp_path, user_id=777)
    b = TelegramBot(site, make_project(tmp_path), FakeLedger(), FakeActions(), str(site.telegram.token_file))
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
    assert [e["kind"] for e in b.ledger.events] == ["rejected"] and "user 12345 in chat 42" in b.ledger.events[0]["text"]
    b.handle_update(msg("/status", user=777))
    b.handle_update(callback("ack:a@demo", user=777))
    assert [c[0] for c in b.actions.calls] == ["status_text", "ack"]
    b.handle_update(msg("/status", chat=7, user=777))
    assert len(b.actions.calls) == 2


def test_alert_send_edits_a_repeat(bot):
    mid = bot.send("hung", "run1", "hung a@demo\nno progress <3 h", alert_buttons("a@demo"), "edr stop a@demo --why hung")
    sent = bot.api.of("sendMessage")[-1]
    assert mid == "1" and sent["disable_notification"] is False
    assert sent["text"] == "<b>demo · hung a@demo</b>\nno progress &lt;3 h\n<code>edr stop a@demo --why hung</code>"
    assert [b["callback_data"] for b in sent["reply_markup"]["inline_keyboard"][0]] == ["keep12:a@demo", "ack:a@demo"]
    assert bot.send("hung", "run1", "no progress for 3 h") == "1"
    assert bot.api.of("editMessageText")[-1]["message_id"] == 1
    assert bot.send("hung", "run2", "x") == "2"
    bot.edit("2", "resolved <ok>")
    assert bot.api.of("editMessageText")[-1]["text"] == "<b>demo · resolved &lt;ok&gt;</b>"


def test_board_is_created_once_then_edited(bot, tmp_path):
    bot.board("board v1")
    sent = bot.api.of("sendMessage")
    assert len(sent) == 1 and sent[0]["disable_notification"] is True
    assert bot.api.of("pinChatMessage")[0]["message_id"] == 1
    assert bot.ledger.kv["telegram"]["board"] == 1 and not (tmp_path / "data").exists()
    bot.board("board v2")
    assert len(bot.api.of("sendMessage")) == 1
    edit = bot.api.of("editMessageText")[-1]
    assert edit["message_id"] == 1 and edit["reply_markup"] is None
    assert re.fullmatch(r"<b>demo · board \d\d:\d\d</b>\n<pre>board v2</pre>", edit["text"])
    # A new bot on the same ledger edits the same message.
    again = TelegramBot(bot.site, bot.project, bot.ledger, bot.actions, str(bot.tg.token_file))
    again.api = FakeApi()
    again.board("board v3")
    assert again.api.of("sendMessage") == [] and again.api.of("editMessageText")[-1]["message_id"] == 1
    # /board unpins the old message and pins a new one.
    bot.handle_update(msg("/board"))
    assert bot.api.of("unpinChatMessage")[-1]["message_id"] == 1
    assert bot.api.of("pinChatMessage")[-1]["message_id"] == 2


def test_api_obeys_429_retry_after(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    b = TelegramBot(site, make_project(tmp_path), FakeLedger(), FakeActions(), str(site.telegram.token_file))
    slept: list[int] = []
    monkeypatch.setattr(tgmod.time, "sleep", slept.append)
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

    monkeypatch.setattr(tgmod, "urlopen", fake_urlopen)
    assert b.api("sendMessage", {"chat_id": CHAT, "text": "x"}) == {"message_id": 9}
    assert slept == [3]
    answers.append(urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"description":"Bad Request: message is not modified"}')))
    assert b.api("editMessageText", {"chat_id": CHAT}) == {}
    answers.append(urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"description":"Bad Request: chat not found"}')))
    with pytest.raises(tgmod.ApiError, match="chat not found"):
        b.api("sendMessage", {"chat_id": CHAT})


def test_api_retries_idempotent_methods_only(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    b = TelegramBot(site, make_project(tmp_path), FakeLedger(), FakeActions(), str(site.telegram.token_file))
    slept: list[int] = []
    monkeypatch.setattr(tgmod.time, "sleep", slept.append)
    tries: list[str] = []

    def fake_urlopen(req, timeout):
        tries.append(req.full_url.rsplit("/", 1)[1])
        raise TimeoutError("timed out")

    monkeypatch.setattr(tgmod, "urlopen", fake_urlopen)
    with pytest.raises(tgmod.ApiError, match="sendMessage: timed out"):
        b.api("sendMessage", {"chat_id": CHAT, "text": "x"})
    assert tries == ["sendMessage"] and slept == []
    for method in ("getUpdates", "editMessageText", "answerCallbackQuery"):
        tries.clear()
        with pytest.raises(tgmod.ApiError, match=method):
            b.api(method, {"chat_id": CHAT})
        assert tries == [method] * 3
    assert slept == [5, 10, 15] * 3


def test_builtin_commands_land_in_events(bot):
    bot.handle_update(msg("/board"))
    bot.handle_update(msg("/keep 'a;rm' 3"))
    assert [(e["kind"], e["text"][:6]) for e in bot.ledger.events] == [("command", "/board"), ("refused", "/keep ")]


def test_poll_survives_a_bad_response(bot, monkeypatch):
    calls: list[str] = []

    def api(method, params, files=None):
        calls.append(method)
        if len(calls) == 1:
            raise ValueError("not json")
        bot._stop.set()
        return []

    monkeypatch.setattr(bot, "api", api)
    monkeypatch.setattr(tgmod.time, "sleep", lambda s: None)
    bot._poll()
    assert calls == ["getUpdates", "getUpdates"]


def test_api_turns_a_bad_body_into_apierror(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    b = TelegramBot(site, make_project(tmp_path), FakeLedger(), FakeActions(), str(site.telegram.token_file))
    monkeypatch.setattr(tgmod.time, "sleep", lambda s: None)
    html = b"<html>502 Bad Gateway</html>"
    answers = [urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(html))] + [io.BytesIO(html) for _ in range(3)]

    def fake_urlopen(req, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(tgmod, "urlopen", fake_urlopen)
    with pytest.raises(tgmod.ApiError, match="502"):
        b.api("getUpdates", {"offset": None})
    with pytest.raises(tgmod.ApiError, match="Expecting value"):
        b.api("getUpdates", {"offset": None})
    assert answers == []


def test_custom_output_keeps_a_non_utf8_byte(bot):
    bot.tg.commands["latin"] = BotCommand("latin", "latin", ["printf", "caf\\xe9\\n"])
    bot.handle_update(msg("/latin"))
    assert last_reply(bot) == pre("caf\ufffd")


def test_pre_escapes_and_cuts():
    assert pre("a<b>&") == "<pre>a&lt;b&gt;&amp;</pre>"
    assert len(pre("x" * 5000)) == 4000 + len("<pre></pre>")

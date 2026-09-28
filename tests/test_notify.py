"""The ntfy and mail channels against a local HTTP server and a mocked SMTP; every write goes to tmp_path."""

from __future__ import annotations

import io
import json
import shutil
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from helpers_telegram import FakeApi, FakeDatabase, make_project, make_site, make_user

from edarunner import cli, config
from edarunner.config import ConfigError
from edarunner.model import Mail, Ntfy, User
from edarunner.notify import make_notifiers, untag, wanted
from edarunner.notify.alerts import Alert, button
from edarunner.notify.mail import MailNotifier
from edarunner.notify.ntfy import NtfyNotifier
from edarunner.notify.telegram import TelegramBot
from helpers_driver import DEMO

PROJECT = SimpleNamespace(project="demo")


@pytest.fixture
def ntfy_server():
    """A local ntfy stand-in: records (path, auth header, JSON body) per POST and answers 200."""
    got: list[tuple[str, str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, self.headers.get("Authorization", ""), json.loads(body)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", got
    server.shutdown()


def secret(tmp_path: Path, name: str, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_text("s3cret\n")
    path.chmod(mode)
    return path


BUTTONS = [button(x, "demo", handle="a@demo", why="hung") for x in ("stop", "keep6", "keep12", "keep24")]


def dead(buttons: bool = True) -> Alert:
    return Alert("dead", "r1", "driver gone for", "a@demo", "no heartbeat", todo=[("Look:", "edr status a@demo")],
                 buttons=BUTTONS if buttons else [])


def test_ntfy_sends_alerts_with_priority_and_button_lines(ntfy_server, tmp_path):
    url, got = ntfy_server
    n = NtfyNotifier(PROJECT, Ntfy(topic="edr-x", url=url, token_file=secret(tmp_path, "t")))
    assert n.send(dead()) is None
    path, auth, body = got[0]
    assert path == "/" and auth == "Bearer s3cret"
    assert body == {"topic": "edr-x", "title": "🔴 demo: driver gone for a@demo", "priority": 5, "message":
                    "no heartbeat\n\nLook:\nedr status a@demo\n\nStop: edr stop a@demo --after-task --why hung\n"
                    "+6h: edr keep a@demo --hours 6\n+12h: edr keep a@demo --hours 12\n+24h: edr keep a@demo --hours 24",
                    "actions": [{"action": "copy", "label": "Stop", "value": "edr stop a@demo --after-task --why hung"},
                                {"action": "copy", "label": "+6h", "value": "edr keep a@demo --hours 6"},
                                {"action": "copy", "label": "+12h", "value": "edr keep a@demo --hours 12"}]}
    n.send(Alert("hung", "r1", "no progress in", "a@demo", "stuck"))
    assert got[1][2]["priority"] == 4 and got[1][2]["message"] == "stuck" and "actions" not in got[1][2]
    assert n.post("note", "a &amp; <b>b</b>", silent=True)
    assert got[2][2]["message"] == "a & b" and got[2][2]["priority"] == 1 and got[2][2]["title"] == "demo: note"
    assert not NtfyNotifier(PROJECT, Ntfy(topic="x", url="http://127.0.0.1:9")).post("note", "x")


def test_mail_sends_one_mail_per_alert():
    cfg = Mail(host="smtp.example.org", sender="edr@example.org", to=["a@example.org", "b@example.org"])
    with mock.patch("smtplib.SMTP") as smtp:
        MailNotifier(PROJECT, cfg).send(dead())
        s = smtp.return_value.__enter__.return_value
        smtp.assert_called_once_with("smtp.example.org", 587, timeout=30)
        s.starttls.assert_called_once()
        s.login.assert_not_called()
        msg = s.send_message.call_args[0][0]
        assert msg["Subject"] == "🔴 demo: driver gone for a@demo" and msg["To"] == "a@example.org, b@example.org"
        assert msg.get_content().splitlines()[:5] == ["no heartbeat", "", "Look:", "    edr status a@demo", ""]
        s.send_message.side_effect = OSError("refused")
        assert not MailNotifier(PROJECT, cfg).post("note", "x")


def test_mail_logs_in_with_the_password_file(tmp_path):
    cfg = Mail(host="h", sender="edr@example.org", to=["a@example.org"], port=25, starttls=False,
               password_file=secret(tmp_path, "pw"))
    with mock.patch("smtplib.SMTP") as smtp:
        assert MailNotifier(PROJECT, cfg).post("note", "hi &lt;there&gt;")
        s = smtp.return_value.__enter__.return_value
        s.starttls.assert_not_called()
        s.login.assert_called_once_with("edr@example.org", "s3cret")
        assert s.send_message.call_args[0][0].get_content() == "hi <there>\n"


def test_make_notifiers_builds_every_channel_with_a_private_secret(tmp_path):
    user = User(path=tmp_path / "user.toml", ntfy=Ntfy(topic="t"), mail=Mail(host="h", sender="f", to=["t"]),
                alerts=["done"])
    made = make_notifiers(user, None, PROJECT, None)
    assert [type(n) for n in made] == [NtfyNotifier, MailNotifier] and all(n.kinds == {"done"} for n in made)
    # Every channel gets every alert; an opt-in kind goes only where the one list of the user file asks for it.
    assert wanted(made, "dead") == wanted(made, "done") == made and wanted(made, "metrics") == []
    user.ntfy.token_file = secret(tmp_path, "t", 0o644)
    user.mail.password_file = tmp_path / "absent"
    assert make_notifiers(user, None, PROJECT, None) == []


def demo_user(tmp_path: Path, monkeypatch, text: str) -> Path:
    """A copy of the demo with HOME at tmp_path, whose user.toml holds `text`."""
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(root)
    user = Path(config.DEFAULT_USER).expanduser()
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(text)
    return root


def test_the_user_file_holds_the_channels_and_the_site_file_the_bot_commands(tmp_path, monkeypatch):
    root = demo_user(tmp_path, monkeypatch, 'digest_at = "08:00"\n[ntfy]\ntopic = "edr-x"\ntoken_file = "ntfy.token"\n'
                                            '\n[mail]\nhost = "smtp.example.org"\nfrom = "edr@example.org"\nto = ["a@example.org"]\n'
                                            '\n[telegram]\nchat_id = 5\ntopics = true\n')
    user, path = config.load_user(), Path(config.DEFAULT_USER).expanduser()
    assert user.ntfy == Ntfy(topic="edr-x", url="https://ntfy.sh", token_file=path.parent / "ntfy.token")
    assert user.mail.sender == "edr@example.org" and user.mail.port == 587 and user.mail.starttls
    assert user.digest_at == "08:00" and user.telegram.topics and user.telegram.token_file == path.parent / "telegram.token"
    for bad, match in (('[mail]\nhost = "h"\nto = ["a"]\n', "missing key 'mail.from'"),
                       ('[mail]\nhost = "h"\nfrom = "f"\nto = "a"\n', "mail.to must be list, not str"),
                       ('[ntfy]\ntopic = "t"\npriority = 5\n', "unknown key 'ntfy.priority'"),
                       ('digest_at = "8:00"\n', "digest_at must be HH:MM or empty"),
                       ('[telegram]\ntopic_id = 17\nchat_id = 5\n', "unknown key 'telegram.topic_id'")):
        path.write_text(bad)
        with pytest.raises(ConfigError, match=match):
            config.load_user()
    path.unlink()
    assert config.load_user() == User(path=path)  # no file, no channel
    site = root / "site.toml"
    site.write_text(site.read_text() + '\n[telegram.commands.x]\nhelp = "x"\nrun = ["true"]\n')
    assert list(config.load_project(root).site.commands) == ["x"]
    for table in ('[ntfy]\ntopic = "t"', '[mail]\nhost = "h"', "[telegram]\nchat_id = 5"):
        site.write_text((DEMO / "site.toml").read_text() + "\n" + table + "\n")
        with pytest.raises(ConfigError, match="unknown key '(ntfy|mail|telegram.chat_id)'"):
            config.load_project(root)


def test_two_users_of_one_site_file_have_their_own_chats(tmp_path, monkeypatch):
    for name, chat in (("ann", 1), ("bob", 2)):
        demo_user(tmp_path / name, monkeypatch, f"[telegram]\nchat_id = {chat}\n")
        assert config.load_user().telegram.chat_id == chat


def test_edr_notify_reaches_ntfy_and_mail(ntfy_server, tmp_path, monkeypatch, capsys):
    url, got = ntfy_server
    demo_user(tmp_path, monkeypatch, f'[ntfy]\ntopic = "edr-x"\nurl = "{url}"\n'
                                     '\n[mail]\nhost = "h"\nfrom = "f@example.org"\nto = ["t@example.org"]\n')
    with mock.patch("smtplib.SMTP") as smtp:
        assert cli.main(["notify", "build done"]) == 0
        mail = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert "sent to 2 of 2 notifiers" in capsys.readouterr().out
    assert got[0][2]["message"] == "build done" and mail.get_content() == "build done\n"


@pytest.mark.parametrize("flag,title", [("--board", "demo: board"), ("--digest", "demo: digest")])
def test_edr_notify_sends_the_board_or_the_digest(flag, title, ntfy_server, tmp_path, monkeypatch, capsys):
    url, got = ntfy_server
    demo_user(tmp_path, monkeypatch, f'[ntfy]\ntopic = "edr-x"\nurl = "{url}"\n'
                                     '\n[mail]\nhost = "h"\nfrom = "f@example.org"\nto = ["t@example.org"]\n')
    with mock.patch("smtplib.SMTP") as smtp:
        assert cli.main(["notify", flag]) == 0
        mail = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert "sent to 2 of 2 notifiers" in capsys.readouterr().out
    assert got[0][2]["title"] == mail["Subject"] == title
    assert got[0][2]["message"] + "\n" == mail.get_content() and "<" not in mail.get_content()
    assert cli.main(["notify", "x", flag]) == 1 and cli.main(["notify"]) == 1


def test_ntfy_resends_without_actions_when_the_server_refuses_them(monkeypatch):
    bodies = []

    def urlopen(req, timeout):
        bodies.append(json.loads(req.data))
        if "actions" in bodies[-1] or bodies[-1]["message"] == "bad":
            raise urllib.error.HTTPError(req.full_url, 400, "invalid", {}, io.BytesIO())
        return mock.MagicMock(status=200, __enter__=lambda self: self)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    n = NtfyNotifier(PROJECT, Ntfy(topic="t"))
    copy = [{"action": "copy", "label": "+6h", "value": "edr keep a@demo --hours 6"}]
    assert n.publish("dead a@demo", "x", 5, copy)
    assert [("actions" in b) for b in bodies] == [True, False]
    assert not n.publish("dead a@demo", "bad", 5, copy) and len(bodies) == 4


# Every message kind of the watcher and of edr notify, as its call site makes it:
# (the call, the title after "<project>: ", the text lines, the buttons).
KINDS = {
    "alert": (lambda n: n.send(dead()), "driver gone for a@demo", ["no heartbeat", "", "Look:", "edr status a@demo"],
              BUTTONS),
    "orphan": (lambda n: n.send(Alert("orphan", "orphan:h:7", "tool process with no run on", "h", "sleep 99",
                                      todo=[("End it:", "kill 7")])),
               "tool process with no run on h", ["sleep 99", "", "End it:", "kill 7"], []),
    "watch stale": (lambda n: n.send(Alert("watch", "", "watcher stopped", "", "no watch.json")), "watcher stopped",
                    ["no watch.json"], []),
    "digest": (lambda n: n.post("digest", "<b>Live</b>\n🟢 <code>a@demo</code> pnr"), "digest", ["Live", "🟢 a@demo pnr"], []),
    "board": (lambda n: n.post("board", "🟢 <code>a@demo</code> pnr 3/7\n<i>1 running</i>"), "board",
              ["🟢 a@demo pnr 3/7", "1 running"], []),
    "note": (lambda n: n.post("note", "build &lt;done&gt;"), "note", ["build <done>"], []),
}


def telegram_got(tmp_path, monkeypatch, call):
    """(title, text lines, buttons) of the one message the bot sends; a button is (label, callback_data)."""
    bot = TelegramBot(make_user(tmp_path).telegram, make_site(), make_project(tmp_path), FakeDatabase(), None,
                      str(tmp_path / "telegram.token"))
    bot.api = FakeApi()
    call(bot)
    [sent] = bot.api.of("sendMessage")
    title, *lines = untag(sent["text"]).splitlines()
    keys = (sent.get("reply_markup") or {}).get("inline_keyboard") or [[]]
    return title[title.index("demo: "):], lines, [(k["text"], k["callback_data"]) for k in keys[0]]


def ntfy_got(tmp_path, monkeypatch, call):
    """(title, text lines, buttons) of the one push; a button is (label, command) of a copy action, at most three."""
    bodies = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: bodies.append(json.loads(req.data))
                        or mock.MagicMock(status=200, __enter__=lambda self: self))
    call(NtfyNotifier(PROJECT, Ntfy(topic="t")))
    [body] = bodies
    got = [(a["label"], a["value"]) for a in body.get("actions", [])]
    assert all(f"{label}: {c}" in body["message"].splitlines() for label, c in got)
    return body["title"][body["title"].index("demo: "):], body["message"].splitlines(), got


def mail_got(tmp_path, monkeypatch, call):
    """(subject, text lines, buttons) of the one mail; a button is a `label: command` line."""
    with mock.patch("smtplib.SMTP") as smtp:
        call(MailNotifier(PROJECT, Mail(host="h", sender="f@example.org", to=["t@example.org"])))
    [(msg,), _] = smtp.return_value.__enter__.return_value.send_message.call_args
    lines = [ln.strip() for ln in msg.get_content().splitlines()]
    labels = {b[0] for b in BUTTONS}
    return msg["Subject"][msg["Subject"].index("demo: "):], lines, [
        tuple(ln.split(": ", 1)) for ln in lines if ln.split(": ", 1)[0] in labels]


EXPECT = {telegram_got: lambda bs: [(b[0], b[1]) for b in bs], ntfy_got: lambda bs: [(b[0], b[2]) for b in bs][:3],
          mail_got: lambda bs: [(b[0], b[2]) for b in bs]}


@pytest.mark.parametrize("channel", [telegram_got, ntfy_got, mail_got], ids=["telegram", "ntfy", "mail"])
@pytest.mark.parametrize("kind", KINDS)
def test_every_channel_carries_every_kind(kind, channel, tmp_path, monkeypatch):
    call, title, lines, buttons = KINDS[kind]
    got_title, got_lines, got_buttons = channel(tmp_path, monkeypatch, call)
    assert got_title == f"demo: {title}"
    assert got_lines[: len(lines)] == lines
    assert got_buttons == EXPECT[channel](buttons)

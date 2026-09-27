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
from helpers_telegram import FakeActions, FakeApi, FakeDatabase, make_project, make_site

from edarunner import cli, config
from edarunner.config import ConfigError
from edarunner.model import Mail, Ntfy
from edarunner.notify import BUTTON_CMDS, alert_buttons, make_notifiers, untag
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


def test_ntfy_sends_alerts_with_priority_and_button_lines(ntfy_server, tmp_path):
    url, got = ntfy_server
    n = NtfyNotifier(PROJECT, Ntfy(topic="edr-x", url=url, token_file=secret(tmp_path, "t")))
    assert n.send("dead", "r1", "dead a@demo\nno heartbeat", alert_buttons("a@demo"), "edr status a@demo") is None
    path, auth, body = got[0]
    assert path == "/" and auth == "Bearer s3cret"
    assert body == {"topic": "edr-x", "title": "demo: dead a@demo", "priority": 5, "message":
                    "no heartbeat\nedr status a@demo\nkeep 12h: edr keep a@demo --hours 12\n"
                    "ack: edr keep a@demo --ack\nstop: edr stop a@demo --after-task",
                    "actions": [{"action": "copy", "label": "keep 12h", "value": "edr keep a@demo --hours 12"},
                                {"action": "copy", "label": "ack", "value": "edr keep a@demo --ack"},
                                {"action": "copy", "label": "stop", "value": "edr stop a@demo --after-task"}]}
    n.send("hung", "r1", "hung a@demo")
    assert got[1][2]["priority"] == 4 and got[1][2]["message"] == "hung a@demo" and "actions" not in got[1][2]
    assert n.post("note", "a &amp; <b>b</b>", silent=True)
    assert got[2][2]["message"] == "a & b" and got[2][2]["priority"] == 1
    assert not NtfyNotifier(PROJECT, Ntfy(topic="x", url="http://127.0.0.1:9")).post("note", "x")


def test_mail_sends_one_mail_per_alert():
    cfg = Mail(host="smtp.example.org", sender="edr@example.org", to=["a@example.org", "b@example.org"])
    with mock.patch("smtplib.SMTP") as smtp:
        MailNotifier(PROJECT, cfg).send("failed", "r1", "failed a@demo\nexit 1", alert_buttons("a@demo"))
        s = smtp.return_value.__enter__.return_value
        smtp.assert_called_once_with("smtp.example.org", 587, timeout=30)
        s.starttls.assert_called_once()
        s.login.assert_not_called()
        msg = s.send_message.call_args[0][0]
        assert msg["Subject"] == "demo: failed a@demo" and msg["To"] == "a@example.org, b@example.org"
        assert msg.get_content().splitlines()[:2] == ["exit 1", "keep 12h: edr keep a@demo --hours 12"]
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
    site = SimpleNamespace(telegram=None, ntfy=Ntfy(topic="t"), mail=Mail(host="h", sender="f", to=["t"]))
    kinds = [type(n) for n in make_notifiers(site, PROJECT, None, None)]
    assert kinds == [NtfyNotifier, MailNotifier]
    site.ntfy.token_file = secret(tmp_path, "t", 0o644)
    site.mail.password_file = tmp_path / "absent"
    assert make_notifiers(site, PROJECT, None, None) == []


def demo_site(tmp_path: Path, extra: str) -> Path:
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    site = root / "site.toml"
    site.write_text(site.read_text() + extra)
    return root


def test_site_tables_load(tmp_path):
    root = demo_site(tmp_path, '\n[ntfy]\ntopic = "edr-x"\ntoken_file = "ntfy.token"\n'
                               '\n[mail]\nhost = "smtp.example.org"\nfrom = "edr@example.org"\nto = ["a@example.org"]\n')
    site = config.load_project(root).site
    assert site.ntfy == Ntfy(topic="edr-x", url="https://ntfy.sh", token_file=root / "ntfy.token")
    assert site.mail.sender == "edr@example.org" and site.mail.port == 587 and site.mail.starttls
    for bad, match in (('[mail]\nhost = "h"\nto = ["a"]\n', "missing key 'mail.from'"),
                       ('[mail]\nhost = "h"\nfrom = "f"\nto = "a"\n', "mail.to must be list, not str"),
                       ('[ntfy]\ntopic = "t"\npriority = 5\n', "unknown key 'ntfy.priority'")):
        (root / "site.toml").write_text((DEMO / "site.toml").read_text() + "\n" + bad)
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)


def test_edr_notify_reaches_ntfy_and_mail(ntfy_server, tmp_path, monkeypatch, capsys):
    url, got = ntfy_server
    root = demo_site(tmp_path, f'\n[ntfy]\ntopic = "edr-x"\nurl = "{url}"\n'
                               '\n[mail]\nhost = "h"\nfrom = "f@example.org"\nto = ["t@example.org"]\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(root)
    with mock.patch("smtplib.SMTP") as smtp:
        assert cli.main(["notify", "build done"]) == 0
        mail = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert "sent to 2 of 2 notifiers" in capsys.readouterr().out
    assert got[0][2]["message"] == "build done" and mail.get_content() == "build done\n"


@pytest.mark.parametrize("flag,title", [("--board", "demo: board"), ("--digest", "demo: digest")])
def test_edr_notify_sends_the_board_or_the_digest(flag, title, ntfy_server, tmp_path, monkeypatch, capsys):
    url, got = ntfy_server
    root = demo_site(tmp_path, f'\n[ntfy]\ntopic = "edr-x"\nurl = "{url}"\n'
                               '\n[mail]\nhost = "h"\nfrom = "f@example.org"\nto = ["t@example.org"]\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(root)
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
    copy = [{"action": "copy", "label": "ack", "value": "edr keep a@demo --ack"}]
    assert n.publish("dead a@demo", "x", 5, copy)
    assert [("actions" in b) for b in bodies] == [True, False]
    assert not n.publish("dead a@demo", "bad", 5, copy) and len(bodies) == 4


# Every message kind of the watcher and of edr notify, as its call site makes it:
# (the call, the title after "<project>: ", the text lines, the button commands).
BUTTONS = [c.format("a@demo") for c in BUTTON_CMDS.values()]
KINDS = {
    "alert": (lambda n: n.send("dead", "r1", "dead a@demo\nno heartbeat", alert_buttons("a@demo"), "edr status a@demo"),
              "dead a@demo", ["no heartbeat", "edr status a@demo"], BUTTONS),
    "orphan": (lambda n: n.send("orphan", "orphan:h:7", "orphan sleep\nsleep 99", None, "kill -TERM 7"),
               "orphan sleep", ["sleep 99", "kill -TERM 7"], []),
    "watch stale": (lambda n: n.send("watch", "", "watch stale\nno watch.json"), "watch stale", ["no watch.json"], []),
    "digest": (lambda n: n.post("digest", "<b>Live</b>\n🟢 <code>a@demo</code> pnr"), "digest", ["Live", "🟢 a@demo pnr"], []),
    "board": (lambda n: n.post("board", "🟢 <code>a@demo</code> pnr 3/7\n<i>1 running</i>"), "board",
              ["🟢 a@demo pnr 3/7", "1 running"], []),
    "note": (lambda n: n.post("note", "build &lt;done&gt;"), "note", ["build <done>"], []),
}


def telegram_got(tmp_path, monkeypatch, call):
    """(title, text lines, button commands) of the one message the bot sends; a button is a callback."""
    bot = TelegramBot(make_site(tmp_path), make_project(tmp_path), FakeDatabase(), FakeActions(),
                      str(tmp_path / "telegram.token"))
    bot.api = FakeApi()
    call(bot)
    [sent] = bot.api.of("sendMessage")
    title, *lines = untag(sent["text"]).splitlines()
    keys = (sent.get("reply_markup") or {}).get("inline_keyboard") or [[]]
    cmds = [BUTTON_CMDS[k["callback_data"].split(":")[0]].format(k["callback_data"].split(":")[1]) for k in keys[0]]
    return title[title.index("demo: "):], lines, cmds  # an alert title starts with the mark of its state


def ntfy_got(tmp_path, monkeypatch, call):
    """(title, text lines, button commands) of the one push; each command is also a text line."""
    bodies = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: bodies.append(json.loads(req.data))
                        or mock.MagicMock(status=200, __enter__=lambda self: self))
    call(NtfyNotifier(PROJECT, Ntfy(topic="t")))
    [body] = bodies
    cmds = [a["value"] for a in body.get("actions", [])]
    assert all(any(line.endswith(": " + c) for line in body["message"].splitlines()) for c in cmds)
    return body["title"], body["message"].splitlines(), cmds


def mail_got(tmp_path, monkeypatch, call):
    """(subject, text lines, button commands) of the one mail; a button is a `label: command` line."""
    with mock.patch("smtplib.SMTP") as smtp:
        call(MailNotifier(PROJECT, Mail(host="h", sender="f@example.org", to=["t@example.org"])))
    [(msg,), _] = smtp.return_value.__enter__.return_value.send_message.call_args
    lines = msg.get_content().splitlines()
    return msg["Subject"], lines, [ln.split(": ", 1)[1] for ln in lines if ln.startswith(("keep 12h:", "ack:", "stop:"))]


@pytest.mark.parametrize("channel", [telegram_got, ntfy_got, mail_got], ids=["telegram", "ntfy", "mail"])
@pytest.mark.parametrize("kind", KINDS)
def test_every_channel_carries_every_kind(kind, channel, tmp_path, monkeypatch):
    call, title, lines, buttons = KINDS[kind]
    got_title, got_lines, got_buttons = channel(tmp_path, monkeypatch, call)
    assert got_title == f"demo: {title}"
    assert got_lines[: len(lines)] == lines
    assert got_buttons == buttons

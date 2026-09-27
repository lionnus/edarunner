"""The ntfy and mail channels against a local HTTP server and a mocked SMTP; every write goes to tmp_path."""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from edarunner import cli, config
from edarunner.config import ConfigError
from edarunner.model import Mail, Ntfy
from edarunner.notify import alert_buttons, make_notifiers
from edarunner.notify.mail import MailNotifier
from edarunner.notify.ntfy import NtfyNotifier

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"
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
                    "ack: edr keep a@demo --ack\nstop: edr stop a@demo --after-task"}
    n.send("hung", "r1", "hung a@demo")
    assert got[1][2]["priority"] == 4 and got[1][2]["message"] == "hung a@demo"
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

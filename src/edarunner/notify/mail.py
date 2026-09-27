"""The mail channel: one mail per alert and per `edr notify` through one SMTP server."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from edarunner.model import Mail
from edarunner.notify import Button, Notifier, plain, untag

log = logging.getLogger(__name__)


class MailNotifier(Notifier):
    """One SMTP server and its recipients; buttons as command lines, no edits, the board on request only."""

    def __init__(self, project: Any, cfg: Mail) -> None:
        self.project = project
        self.cfg = cfg
        self.password = Path(cfg.password_file).expanduser().read_text().strip() if cfg.password_file else None

    def mail(self, subject: str, text: str) -> bool:
        """Send one plain-text mail; True when the server took it."""
        msg = EmailMessage()
        msg["Subject"] = f"{self.project.project}: {subject}"
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.to)
        msg.set_content(text)
        try:
            with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=30) as smtp:
                if self.cfg.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.password is not None:
                    smtp.login(self.cfg.user or self.cfg.sender, self.password)
                smtp.send_message(msg)
            return True
        except (OSError, smtplib.SMTPException) as e:
            log.warning("mail: %s", e)
            return False

    def send(self, kind: str, run_id: str, text: str, buttons: list[Button] | None = None,
             cmd: str | None = None) -> str | None:
        """Mail one alert: the first line of `text` is the subject; a button becomes a line with its command."""
        title, _, rest = text.partition("\n")
        self.mail(title, plain(rest or title, buttons, cmd))
        return None

    def post(self, title: str, html: str, silent: bool = False) -> bool:
        return self.mail(title, untag(html))

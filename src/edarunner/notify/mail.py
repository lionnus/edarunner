"""The mail channel: one mail per alert and per `edr notify` through one SMTP server."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from edarunner.model import Mail
from edarunner.notify import Notifier, alerts, untag

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
        msg["Subject"] = subject
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

    def send(self, alert: alerts.Alert) -> str | None:
        """Mail one alert: the first line is the subject, a command sits indented, a button is a command line."""
        self.mail(alerts.subject(self.project.project, alert), alerts.text(alert, "    "))
        return None

    def post(self, title: str, html: str, silent: bool = False) -> bool:
        return self.mail(f"{self.project.project}: {title}", untag(html))

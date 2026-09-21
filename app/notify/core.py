"""Notifier core — SMTP when configured, log otherwise.

Public surface is one function, ``notify(subject, body)``, and one class,
``Notifier``, so tests can inject a fake SMTP factory.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Optional, Protocol

from app.config import Settings, get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


class _SMTPLike(Protocol):
    def starttls(self) -> object: ...
    def login(self, user: str, password: str) -> object: ...
    def send_message(self, msg: EmailMessage) -> object: ...
    def quit(self) -> object: ...


SMTPFactory = Callable[[str, int], _SMTPLike]


def _default_smtp_factory(host: str, port: int) -> _SMTPLike:
    return smtplib.SMTP(host, port, timeout=15)


@dataclass
class Notifier:
    """Send notifications via SMTP or fall back to structured logging.

    Args:
        settings: Optional pre-built Settings (tests may override).
        smtp_factory: Factory that returns an SMTP-like client (tests inject).
    """

    settings: Optional[Settings] = None
    smtp_factory: SMTPFactory = _default_smtp_factory

    def _cfg(self) -> Settings:
        return self.settings or get_settings()

    def notify(self, subject: str, body: str) -> str:
        s = self._cfg()
        host = (s.smtp_host or "").strip()
        sender = (s.smtp_from or s.smtp_user or "").strip()
        recipient = sender  # self-send by default; extend when we add a "to" field
        if not host or not sender:
            log.warning("[notify:log-only] %s\n%s", subject, body)
            return "log"

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content(body)

        try:
            client = self.smtp_factory(host, int(s.smtp_port or 587))
            if s.smtp_starttls:
                client.starttls()
            user = (s.smtp_user or "").strip()
            password = s.smtp_password.get_secret_value() if s.smtp_password else ""
            if user and password:
                client.login(user, password)
            client.send_message(msg)
            client.quit()
            log.info("notify sent subject=%r via %s", subject, host)
            return "smtp"
        except Exception as exc:  # noqa: BLE001
            log.warning("smtp notify failed (%s); falling back to log: %s\n%s",
                        exc, subject, body)
            return "error"


def notify(subject: str, body: str) -> str:
    """Module-level convenience — always uses live Settings."""
    return Notifier().notify(subject, body)

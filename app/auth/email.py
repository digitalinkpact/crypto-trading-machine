"""Async SMTP sender. Falls back to logging the message when SMTP is unset."""
from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


async def send(*, to: str, subject: str, body: str) -> None:
    s = get_settings()
    sender = s.smtp_from or s.smtp_user
    if not s.smtp_host or not sender:
        # No SMTP configured — log it so operators can recover the link
        # while testing. Never silently swallow.
        log.warning(
            "SMTP not configured — would email %s | subject=%r | body=\n%s",
            to, subject, body,
        )
        return

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user or None,
            password=s.smtp_password.get_secret_value() or None,
            start_tls=s.smtp_starttls,
            timeout=20,
        )
        log.info("sent email to %s subject=%r", to, subject)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to send email to %s: %s", to, exc)
        raise

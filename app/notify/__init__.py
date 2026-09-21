"""Notifier facade — logs, SMTP, or no-op depending on Settings.

Never raises: notification failures must never propagate into the caller
(scheduler jobs, verdict pipeline). All failures are logged.
"""
from .core import Notifier, notify

__all__ = ["Notifier", "notify"]

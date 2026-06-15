"""Stdlib logging configuration for the app."""
from __future__ import annotations

import logging
import sys

from app.config import get_settings

_CONFIGURED = False


def _resolve_level(raw: str) -> int:
    """Map a configured level (name or numeric string) to a logging level int.

    Falls back to INFO for unknown values so a typo in LOG_LEVEL can never crash
    startup for an unattended trading bot.
    """
    value = str(raw).strip().upper()
    level = logging.getLevelName(value)  # name -> int, or "Level X" for unknown
    if isinstance(level, int):
        return level
    if value.isdigit():
        return int(value)
    logging.getLogger(__name__).warning(
        "Unknown LOG_LEVEL %r; falling back to INFO", raw
    )
    return logging.INFO


def configure_logging() -> None:
    """Idempotent root logger setup."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s — %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(_resolve_level(settings.log_level))
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)

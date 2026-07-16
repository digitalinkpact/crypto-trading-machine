"""Stdlib logging configuration for the app."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings

_CONFIGURED = False

# Durable log file managed by the app itself (rotated), independent of however
# the process's stdout happens to be redirected (nohup, systemd, etc.). Without
# this, an externally-redirected `>> logs/uvicorn.log` grows unbounded and,
# more importantly, stops being a reliable signal the moment someone forgets
# the redirect or switches launch methods.
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "uvicorn.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_BACKUP_COUNT = 5


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
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stream_handler)

    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # noqa: BLE001
        # Never let a log-directory permission issue stop the trading loop.
        logging.getLogger(__name__).warning(
            "could not attach rotating file handler at %s: %s", _LOG_FILE, exc
        )

    root.setLevel(_resolve_level(settings.log_level))
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)

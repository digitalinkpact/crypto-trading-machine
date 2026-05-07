"""Credential & settings persistence — writes config to .env without duplicates."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.config import get_settings

_ENV_PATH = Path(".env")


def _write_env(updates: dict[str, str], drop: Iterable[str] = ()) -> None:
    drop_set = set(drop) | set(updates.keys())
    existing: list[str] = []
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            stripped = line.lstrip()
            if any(stripped.startswith(f"{k}=") for k in drop_set):
                continue
            existing.append(line)
    for k, v in updates.items():
        existing.append(f"{k}={v}")
    _ENV_PATH.write_text("\n".join(existing).rstrip() + "\n")
    get_settings.cache_clear()


def save_binance_credentials(api_key: str, api_secret: str) -> None:
    """Persist API credentials to .env, then refresh in-process settings."""
    api_key = api_key.strip()
    api_secret = api_secret.strip()
    if not api_key or not api_secret:
        raise ValueError("API key and secret are required")
    _write_env({
        "BINANCE_API_KEY": api_key,
        "BINANCE_API_SECRET": api_secret,
    })


def save_trading_mode(paper: bool) -> None:
    """Flip between PAPER and LIVE. PAPER also sets dry_run=true (defense in depth)."""
    if paper:
        _write_env({"PAPER_TRADING": "true", "DRY_RUN": "true"})
    else:
        _write_env({"PAPER_TRADING": "false", "DRY_RUN": "false"})


def credentials_present() -> bool:
    s = get_settings()
    return bool(
        s.binance_api_key.get_secret_value()
        and s.binance_api_secret.get_secret_value()
    )

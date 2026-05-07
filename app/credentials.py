"""Credential storage — writes API keys to .env without duplicating lines."""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings

_ENV_PATH = Path(".env")
_KEYS = ("BINANCE_API_KEY", "BINANCE_API_SECRET")


def save_binance_credentials(api_key: str, api_secret: str) -> None:
    """Persist API credentials to .env, then refresh in-process settings."""
    api_key = api_key.strip()
    api_secret = api_secret.strip()
    if not api_key or not api_secret:
        raise ValueError("API key and secret are required")

    existing: list[str] = []
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            stripped = line.lstrip()
            if any(stripped.startswith(f"{k}=") for k in _KEYS):
                continue
            existing.append(line)

    existing.append(f"BINANCE_API_KEY={api_key}")
    existing.append(f"BINANCE_API_SECRET={api_secret}")
    _ENV_PATH.write_text("\n".join(existing).rstrip() + "\n")

    # Refresh the cached Settings so the running process picks up new keys.
    get_settings.cache_clear()


def credentials_present() -> bool:
    s = get_settings()
    return bool(
        s.binance_api_key.get_secret_value()
        and s.binance_api_secret.get_secret_value()
    )

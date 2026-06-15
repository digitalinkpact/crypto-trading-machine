"""Credential & settings persistence — writes config to .env without duplicates."""
from __future__ import annotations

import contextlib
import os
import tempfile
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
    content = "\n".join(existing).rstrip() + "\n"
    # Atomic write with restrictive perms: .env holds API secrets, so it must
    # never be world-readable, and a crash mid-write must not truncate it.
    directory = _ENV_PATH.resolve().parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, _ENV_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
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
        _write_env({"LIVE_MODE": "false", "PAPER_TRADING": "true", "DRY_RUN": "true"})
    else:
        _write_env({"LIVE_MODE": "true", "PAPER_TRADING": "false", "DRY_RUN": "false"})


def credentials_present() -> bool:
    s = get_settings()
    return bool(
        s.binance_api_key.get_secret_value()
        and s.binance_api_secret.get_secret_value()
    )


# ── Tunable risk knobs (written to .env so they survive restarts) ──────────


def _to_int(raw: str) -> int:
    """Parse an int, tolerating whole-number decimals like '6.0' from form inputs."""
    f = float(raw)
    if not f.is_integer():
        raise ValueError(f"expected a whole number, got {raw!r}")
    return int(f)


_RISK_KEYS = {
    "stop_loss_pct":                  ("STOP_LOSS_PCT",                  float,   (0.005, 0.20)),
    "take_profit_pct":                ("TAKE_PROFIT_PCT",                float,   (0.005, 0.50)),
    "trailing_stop_pct":              ("TRAILING_STOP_PCT",              float,   (0.005, 0.20)),
    "max_hold_hours":                 ("MAX_HOLD_HOURS",                 _to_int, (1, 10000)),
    "drawdown_circuit_breaker_pct":   ("DRAWDOWN_CIRCUIT_BREAKER_PCT",   float,   (0.01, 0.50)),
    "min_signal_confidence":          ("MIN_SIGNAL_CONFIDENCE",          float,   (0.0, 1.0)),
    "max_position_pct":               ("MAX_POSITION_PCT",               float,   (0.005, 1.0)),
    "max_open_positions":             ("MAX_OPEN_POSITIONS",             _to_int, (1, 25)),
    "ml_gate_threshold":              ("ML_GATE_THRESHOLD",              float,   (0.0, 1.0)),
}


def save_risk_settings(values: dict[str, str]) -> dict[str, float | int]:
    """Validate & persist a subset of risk knobs. Returns the parsed values."""
    parsed: dict[str, float | int] = {}
    env_updates: dict[str, str] = {}
    for field_name, (env_key, caster, (lo, hi)) in _RISK_KEYS.items():
        raw = values.get(field_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            v = caster(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"{field_name}: {exc}") from exc
        if not (lo <= v <= hi):
            raise ValueError(f"{field_name} must be between {lo} and {hi}")
        parsed[field_name] = v
        env_updates[env_key] = str(v)
    if env_updates:
        _write_env(env_updates)
    return parsed

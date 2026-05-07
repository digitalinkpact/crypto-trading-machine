"""Application configuration — single source of truth.

Symbols, timeframes, and risk caps are defined here. Agents, indicators, and
scripts must import from this module rather than hardcoding values.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Timeframe(str, Enum):
    """The 4 canonical timeframes the system trades on."""

    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


# ── Universe ─────────────────────────────────────────────────────────────
# 25 symbols traded on Binance.US (USDT pairs). Adjust as listings change.
# NOTE: Binance.US has fewer listings than Binance.com — verify with
# GET /api/v3/exchangeInfo before adding new symbols.
SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
    "MATICUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT", "UNIUSDT",
    "ETCUSDT", "FILUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SHIBUSDT", "SUIUSDT", "RNDRUSDT", "AAVEUSDT",
)

TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
    Timeframe.W1,
)


class Settings(BaseSettings):
    """Environment-driven settings. Loaded from .env via pydantic-settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Credentials
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"

    # Runtime
    env: str = "dev"
    log_level: str = "INFO"

    # Safety toggles — default to safe values
    dry_run: bool = True
    paper_trading: bool = True

    # Risk caps
    max_position_pct: float = Field(0.10, ge=0.0, le=1.0)
    max_portfolio_risk_pct: float = Field(0.25, ge=0.0, le=1.0)
    kelly_fraction_cap: float = Field(0.25, ge=0.0, le=1.0)

    # Storage
    data_cache_dir: Path = Path("./data/cache")

    # Binance.US REST endpoint — never point this at binance.com
    binance_base_url: str = "https://api.binance.us"
    binance_ws_url: str = "wss://stream.binance.us:9443"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so Settings is parsed once per process."""
    return Settings()

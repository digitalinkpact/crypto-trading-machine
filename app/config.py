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

    # LLM provider — "deepseek" | "openai" | "groq" | "gemini" | "none"
    llm_provider: str = "deepseek"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    groq_api_key: SecretStr = SecretStr("")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-2.0-flash"

    # Runtime
    env: str = "dev"
    log_level: str = "INFO"

    # Optional dashboard/API guard. If either value is blank, auth is disabled.
    app_basic_auth_user: str = ""
    app_basic_auth_password: SecretStr = SecretStr("")

    # Safety toggles — default to safe values
    dry_run: bool = True
    paper_trading: bool = True

    # Risk caps
    max_position_pct: float = Field(0.05, ge=0.0, le=1.0)        # was 0.10 — safer with 25 coins
    max_portfolio_risk_pct: float = Field(0.25, ge=0.0, le=1.0)
    kelly_fraction_cap: float = Field(0.25, ge=0.0, le=1.0)
    max_open_positions: int = Field(6, ge=1, le=25)              # cap concurrent positions
    max_long_exposure_pct: float = Field(0.60, ge=0.0, le=1.0)   # ≤ 60% of equity in non-USDT

    # Exit gates (hard rules, evaluated every risk-tick)
    stop_loss_pct: float = Field(0.02, ge=0.005, le=0.20)        # 2% hard stop
    take_profit_pct: float = Field(0.05, ge=0.005, le=0.50)      # 5% take-profit
    trailing_stop_pct: float = Field(0.025, ge=0.005, le=0.20)   # 2.5% trail from HWM
    max_hold_hours: int = Field(96, ge=1, le=10000)              # force-exit after 4 days
    drawdown_circuit_breaker_pct: float = Field(0.10, ge=0.01, le=0.50)  # halt new BUYs after -10%

    # Entry gates
    min_signal_confidence: float = Field(0.65, ge=0.0, le=1.0)   # was 0.6
    buy_cooldown_minutes: int = Field(30, ge=0, le=1440)         # was 60

    # Agent thresholds (tunable without code change)
    rsi_oversold: int = Field(25, ge=5, le=50)                   # was 30
    rsi_overbought: int = Field(75, ge=50, le=95)                # was 70
    breakout_lookback: int = Field(30, ge=5, le=200)             # was 20
    vol_contraction_threshold: float = Field(0.65, ge=0.1, le=1.5)  # was 0.7

    # Per-agent weights — multiplies that agent's vote in the aggregator.
    # Demote noisy agents, promote regime + trend-follower.
    agent_weight_trend_follower: float = Field(1.2, ge=0.0, le=3.0)
    agent_weight_mean_reversion: float = Field(1.1, ge=0.0, le=3.0)
    agent_weight_breakout: float = Field(1.0, ge=0.0, le=3.0)
    agent_weight_momentum: float = Field(0.5, ge=0.0, le=3.0)    # demoted — too noisy
    agent_weight_volatility: float = Field(0.8, ge=0.0, le=3.0)
    agent_weight_regime_overlay: float = Field(1.5, ge=0.0, le=3.0)  # promoted
    agent_weight_llm_reasoner: float = Field(1.0, ge=0.0, le=3.0)

    # Adaptive: scale each agent's weight by its rolling win-rate.
    # weight *= clamp(0.5 + win_rate, 0.5, 1.5). Disable for pure deterministic mode.
    adaptive_agent_weights: bool = True

    # ML learning loop (signal outcome labeling + retraining)
    ml_learning_enabled: bool = True
    ml_signal_horizon_minutes: int = Field(240, ge=15, le=10_080)  # default 4h
    ml_min_training_samples: int = Field(200, ge=20, le=1_000_000)
    ml_min_new_labels: int = Field(50, ge=10, le=1_000_000)

    # Storage
    data_cache_dir: Path = Path("./data/cache")

    # Binance.US REST endpoint — never point this at binance.com
    binance_base_url: str = "https://api.binance.us"
    binance_ws_url: str = "wss://stream.binance.us:9443"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so Settings is parsed once per process."""
    return Settings()

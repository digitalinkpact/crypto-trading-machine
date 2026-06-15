
"""Application configuration — single source of truth.

Symbols, timeframes, and risk caps are defined here. Agents, indicators, and
scripts must import from this module rather than hardcoding values.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _REPO_ROOT / ".env"


class Timeframe(str, Enum):
    """The 4 canonical timeframes the system trades on."""

    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"



# ── Universe ─────────────────────────────────────────────────────────────
# Static fallback list for USDT pairs (used if dynamic fetch fails)
STATIC_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
    "POLUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT", "UNIUSDT",
    "ETCUSDT", "FILUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SHIBUSDT", "SUIUSDT", "FETUSDT", "AAVEUSDT",
)

# Back-compat alias — some modules import SYMBOLS directly.
SYMBOLS = STATIC_SYMBOLS

TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
    Timeframe.W1,
)



class Settings(BaseSettings):
    """Environment-driven settings. Loaded from .env via pydantic-settings."""
    # Dynamic symbol discovery
    use_dynamic_symbols: bool = True
    symbols_cache_minutes: int = Field(60, ge=1, le=1440)
    static_symbols: tuple[str, ...] = STATIC_SYMBOLS
    # Universe filters (applied when use_dynamic_symbols=True). Trade every
    # USDT pair on Binance.US except the ones below.
    #  - Leveraged ETF tokens (…UP/DOWN/BULL/BEAR-USDT) are excluded: they decay
    #    and are unsuitable for this strategy.
    #  - Stablecoin→stablecoin pairs (USDCUSDT, …) are excluded: no edge.
    #  - min_quote_volume_usdt is a 24h liquidity floor; 0 = no floor (all coins).
    #    Raise it (e.g. 1_000_000) to skip thin coins with high slippage risk.
    #  - max_symbols caps the universe to the top-N USDT pairs ranked by 24h
    #    quote volume (the most-liquid coins). 0 = no cap. Applied AFTER the
    #    min_quote_volume_usdt floor. The top-N are inherently liquid, so this
    #    doubles as a slippage guard while widening the tradeable universe.
    exclude_leveraged_tokens: bool = True
    min_quote_volume_usdt: float = Field(0.0, ge=0.0)
    max_symbols: int = Field(100, ge=0, le=1000)

    # ── Liquidity-ranked pairlist (multi-stage universe filter) ──────────
    # When `liquidity_pairlist_enabled` is True, the tradable universe is built
    # by a staged pipeline instead of the simple top-N above:
    #   1. take the top `universe_size` USDT pairs by 24h `volume_sort_key`
    #   2. drop pairs with 24h volume < `min_24h_volume` (USDT)
    #   3. drop pairs with fewer than `min_days_listed` days of history
    #   4. drop pairs whose top-of-book spread exceeds `max_spread_percent`
    #   5. keep the top `final_pairlist_size` survivors (volume-ranked)
    # Refreshed every `volume_refresh_seconds`; falls back to
    # fetch_dynamic_symbols then the static list on any API failure.
    #
    # UNIT FOOTGUN: `max_spread_percent` is a PERCENT (0.50 = 0.50%), whereas the
    # execution-time `max_spread_pct` below is a FRACTION (0.0015 = 0.15%). The
    # universe filter is a coarse compute-saver; the execution gate is the hard
    # money-guard and is intentionally kept stricter.
    #
    # SCALE NOTE: these defaults are tuned for Binance.US, which is a *small*
    # exchange — even BTCUSDT trades only ~$2-3M/24h and the ~50th USDT pair is
    # under $2k/24h. binance.com-scale floors (e.g. $5M) would zero the universe.
    # `min_24h_volume` is therefore intentionally low; the spread cap plus the
    # execution-time order-book gate do the real liquidity protection.
    liquidity_pairlist_enabled: bool = True
    universe_size: int = Field(75, ge=1, le=1000)
    min_24h_volume: float = Field(1_000.0, ge=0.0)
    max_spread_percent: float = Field(0.50, ge=0.0, le=100.0)
    min_days_listed: int = Field(15, ge=0, le=10_000)
    final_pairlist_size: int = Field(50, ge=1, le=1000)
    volume_sort_key: str = "quoteVolume"
    volume_refresh_seconds: int = Field(1800, ge=30, le=86_400)
    # Max concurrent per-symbol liquidity probes (depth + listing age).
    liquidity_probe_concurrency: int = Field(8, ge=1, le=50)
    # API rate limit/backoff
    api_retry_attempts: int = Field(3, ge=1, le=10)
    api_retry_backoff_base: int = Field(2, ge=1, le=10)

    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Credentials
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"

    # LLM provider — "deepseek" | "openai" | "groq" | "gemini" | "github" | "none"
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
    # GitHub Models — free tier (rate-limited). PAT with `models:read` scope.
    # Endpoint is OpenAI-compatible. Catalog: https://github.com/marketplace/models
    github_token: SecretStr = SecretStr("")
    github_base_url: str = "https://models.github.ai/inference"
    github_model: str = "openai/gpt-4o"
    # Optional web context for the LLM reasoner (enabled per operator request).
    # When enabled, the LLM agent fetches a small internet snapshot
    # (CoinGecko + DuckDuckGo instant answers) and appends it to the prompt.
    llm_web_enabled: bool = True
    llm_web_timeout_seconds: float = Field(6.0, ge=1.0, le=30.0)
    llm_web_cache_ttl_seconds: int = Field(900, ge=60, le=86_400)
    # When true, the autopilot tick includes the LLM reasoner in the vote.
    # Non-deterministic and rate-limited, but enabled per operator request so
    # live trades benefit from LLM reasoning. Restricted to slow timeframes
    # (D1/W1) with bounded concurrency in app/agents/runner.py.
    llm_in_trading_loop: bool = True

    # Runtime
    env: str = "dev"
    log_level: str = "INFO"

    # ── Auth / sessions / email ──────────────────────────────────────
    # Public base URL used inside emailed links (verify + reset).
    # Example: https://bot.example.com
    base_url: str = "http://localhost:8000"
    # Long random string. Required in production; if empty, a volatile
    # per-process value is generated (sessions die on every restart).
    session_secret: SecretStr = SecretStr("")
    # Cookie lifetime when user ticks "remember me" (days).
    auth_remember_days: int = Field(30, ge=1, le=365)
    # Default cookie lifetime (hours) when "remember me" is unchecked.
    auth_session_hours: int = Field(12, ge=1, le=720)
    # Lockout after N consecutive failures, for M minutes.
    auth_max_failed: int = Field(5, ge=1, le=50)
    auth_lockout_minutes: int = Field(15, ge=1, le=1440)
    # Verify/reset token validity (minutes).
    auth_token_minutes: int = Field(60, ge=5, le=1440)
    # IPs (comma-separated, exact match) that bypass the login wall.
    # Useful for a private LAN, a jump host, or your own static IP.
    # Empty = no bypass. Supports IPv4 only for simplicity.
    auth_ip_allowlist: str = ""
    # Force HTTPS-only cookies + redirect HTTP→HTTPS. Enable when
    # behind a reverse proxy that terminates TLS (nginx/caddy/cloudflare).
    force_https: bool = False

    # SMTP — used for email verification + password resets.
    # Gmail example: smtp.gmail.com / 587 / starttls=true / app password.
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""        # e.g. "Crypto Bot <bot@example.com>"
    smtp_starttls: bool = True

    # Safety toggles — default to safe values
    # Explicit live-mode override. When true, this process is forced to trade
    # live regardless of stale PAPER_TRADING / DRY_RUN values in older envs.
    live_mode: bool = False
    dry_run: bool = True
    paper_trading: bool = True

    @model_validator(mode="after")
    def _apply_live_mode_override(self) -> "Settings":
        if self.live_mode:
            self.paper_trading = False
            self.dry_run = False
        return self

    # Risk caps — fraction-of-equity, single source of truth for sizing/exposure.
    max_position_pct: float = Field(0.05, ge=0.005, le=1.0)      # per-position sizing cap
    max_portfolio_risk_pct: float = Field(0.25, ge=0.0, le=1.0)
    kelly_fraction_cap: float = Field(0.25, ge=0.005, le=1.0)
    max_open_positions: int = Field(25, ge=1, le=25)             # cap concurrent positions
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
    agent_weight_breakout: float = Field(0.5, ge=0.0, le=3.0)    # demoted — net negative PnL in paper
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

    # ML quality gate — drop trades the learned model rates below this win-prob.
    # Closes the learning loop: realized win/loss outcomes train the model,
    # which then filters live BUY/SELL signals. Enabled; until the model has
    # trained on enough labeled data the gate fails open (see max_model_age
    # below). Risk exits bypass this gate.
    ml_gate_enabled: bool = True
    ml_gate_threshold: float = Field(0.50, ge=0.0, le=1.0)
    # Safety valve: an auxiliary quality model trained on a past market regime
    # must not veto trades forever. If the loaded model is older than this many
    # hours, the gate goes advisory (fail-open) so a stale, single-regime model
    # can't permanently freeze entries while the learning loop catches up.
    ml_gate_max_model_age_hours: int = Field(72, ge=1, le=8760)

    # ── Live price stream (websocket) ────────────────────────────────
    # Maintains a sub-second last-price cache from the Binance.US combined
    # miniTicker stream so execution prices aren't 15 minutes stale between
    # OHLCV polls. OHLCV history (needed for indicators) still comes from REST.
    live_price_enabled: bool = True
    # Execution falls back to a REST ticker if the cached price is older than
    # this many seconds (stale-guard against a dropped websocket).
    live_price_max_age_seconds: float = Field(30.0, ge=1.0, le=900.0)

    # ── Order-book / liquidity gate ──────────────────────────────────
    # Before each entry, inspect the live order book and reject fills into thin
    # or wide books. Fails OPEN (allows the trade) if the book can't be fetched.
    orderbook_gate_enabled: bool = True
    orderbook_depth_limit: int = Field(10, ge=5, le=100)
    # Reject entry if (ask-bid)/mid exceeds this (0.0015 = 0.15%).
    max_spread_pct: float = Field(0.0015, ge=0.0, le=0.05)
    # Require resting depth near mid >= this multiple of the trade notional.
    min_depth_trade_multiple: float = Field(2.0, ge=0.0, le=100.0)
    # "Near mid" band used for the depth check (0.001 = 0.1%).
    orderbook_near_pct: float = Field(0.001, ge=0.0001, le=0.05)

    # ── Derivatives context (funding + open interest) ────────────────
    # Binance.US is SPOT-ONLY and has no funding/OI. When enabled, this reads
    # PUBLIC market data from Binance global futures (fapi) for reference only —
    # it never places orders there. Off by default: may be geofenced in the US.
    derivatives_data_enabled: bool = False
    derivatives_base_url: str = "https://fapi.binance.com"
    derivatives_timeout_seconds: float = Field(6.0, ge=1.0, le=30.0)
    derivatives_cache_ttl_seconds: int = Field(300, ge=30, le=3_600)
    # Reject new longs when funding is more negative than this (-0.0001 = -0.01%):
    # deeply negative funding means the perp is crowded-short / squeeze-prone.
    funding_min_pct: float = Field(-0.0001, ge=-0.01, le=0.0)

    # ── Dynamic confidence threshold (online regime) ─────────────────
    # An online logistic model learns from recently-resolved trades and nudges
    # the min-confidence entry bar up (risk-off) or down (risk-on). Bounded so
    # it can never override the technicals-based core by more than a small delta.
    dynamic_threshold_enabled: bool = True
    dynamic_threshold_max_delta: float = Field(0.10, ge=0.0, le=0.30)
    online_regime_min_samples: int = Field(30, ge=10, le=10_000)

    # ── On-chain whale flows (optional) ──────────────────────────────
    # Exchange-inflow spikes are a bearish tell (coins moving to exchanges to
    # be sold). Requires a Glassnode API key; off by default.
    onchain_enabled: bool = False
    glassnode_api_key: SecretStr = SecretStr("")
    onchain_timeout_seconds: float = Field(6.0, ge=1.0, le=30.0)
    onchain_cache_ttl_seconds: int = Field(1800, ge=60, le=86_400)
    # Block new longs when 24h exchange inflow exceeds this z-score vs trailing mean.
    onchain_inflow_spike_z: float = Field(2.0, ge=0.5, le=10.0)

    # Storage
    data_cache_dir: Path = Path("./data/cache")

    # Binance.US REST endpoint — never point this at binance.com
    binance_base_url: str = "https://api.binance.us"
    binance_ws_url: str = "wss://stream.binance.us:9443"

    # Binance.US spot trading fees (tier 0 defaults).
    # See https://www.binance.us/fees — adjust via .env if your tier differs.
    # Market orders pay taker; limit orders that rest on the book pay maker.
    binance_maker_fee: float = Field(0.0040, ge=0.0, le=0.01)
    binance_taker_fee: float = Field(0.0040, ge=0.0, le=0.01)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so Settings is parsed once per process."""
    return Settings()


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
    # Hard blocklist — never opened as a NEW entry regardless of how the
    # universe is sourced (static/dynamic/liquidity-ranked); wired via
    # app/exchange/symbol_source.py's `_apply_blocklist`. Existing risk gates
    # still close any position already open in these symbols; only NEW BUYs
    # are blocked. Empty by default — populate via .env (BLOCKED_SYMBOLS) if a
    # specific coin proves to be a chronic tail-loss producer.
    blocked_symbols: tuple[str, ...] = ()
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

    # Runtime reliability controls.
    # Keep the independent risk loop in-process by default; disable this only
    # when running `python -m app.trading.risk_loop` under its own supervisor.
    risk_loop_in_process_enabled: bool = True
    # Independent risk loop cadence (stop-loss/take-profit/trailing checks).
    risk_manager_loop_seconds: int = Field(15, ge=5, le=300)
    # Maximum wall time for one strategy tick; timed-out ticks are cancelled
    # and treated as unhealthy so watchdog can block new entries and recover.
    autopilot_tick_timeout_seconds: int = Field(50, ge=5, le=600)

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

    # Safety toggles — default to SAFE values (paper trading + dry_run)
    # Explicit live-mode override. When LIVE_MODE=true in .env, this process 
    # is forced to trade live on Binance.US with real money.
    # DEFAULT (no .env or LIVE_MODE not set): paper_trading=True, dry_run=True
    # LIVE MODE (.env: LIVE_MODE=true): paper_trading=False, dry_run=False
    live_mode: bool = False  # SAFE DEFAULT: off
    dry_run: bool = True     # SAFE DEFAULT: on
    paper_trading: bool = True  # SAFE DEFAULT: on
    # Global production kill switch for NEW live entries. This stays false
    # unless an operator explicitly enables it after a documented safety
    # review. It never prevents protective SELLs.
    live_buys_enabled: bool = False

    @model_validator(mode="after")
    def _apply_live_mode_override(self) -> "Settings":
        """If LIVE_MODE=true in .env, force real execution — and NOTHING else.

        LIVE_MODE must only decide *whether orders reach the exchange*, never
        *how much risk is allowed*. An earlier version of this override also
        widened max_open_positions/max_long_exposure_pct and force-disabled
        ml_gate_enabled whenever live mode was on — i.e. flipping one flag to
        "use real money" silently also removed several unrelated risk caps at
        the same time. That is exactly the failure mode this project's own
        audit checklist warns against: enabling live trading must never
        implicitly weaken position limits, exposure caps, or quality gates.
        Every risk parameter is controlled independently via its own setting
        (.env), regardless of live_mode.
        """
        if self.live_mode:
            self.paper_trading = False
            self.dry_run = False
        return self

    # Risk caps — fraction-of-equity, single source of truth for sizing/exposure.
    max_position_pct: float = Field(0.05, ge=0.005, le=1.0)      # per-position sizing cap
    max_portfolio_risk_pct: float = Field(0.25, ge=0.0, le=1.0)
    kelly_fraction_cap: float = Field(0.25, ge=0.005, le=1.0)
    max_open_positions: int = Field(10, ge=1, le=25)              # cap concurrent positions
    max_long_exposure_pct: float = Field(0.60, ge=0.0, le=1.0)   # ≤ 60% of equity in non-USDT
    aggressive_mode_enabled: bool = True
    aggressive_rollback_min_trades: int = Field(30, ge=1, le=10_000)
    aggressive_rollback_min_win_rate: float = Field(0.50, ge=0.0, le=1.0)
    aggressive_max_spread_pct: float = Field(0.0025, ge=0.0, le=0.05)
    rollback_max_spread_pct: float = Field(0.0015, ge=0.0, le=0.05)
    aggressive_position_pct: float = Field(0.06, ge=0.005, le=1.0)
    rollback_position_pct: float = Field(0.03, ge=0.005, le=1.0)
    aggressive_max_open_positions: int = Field(10, ge=1, le=25)
    rollback_max_open_positions: int = Field(10, ge=1, le=25)
    trend_gate_bypass_confidence: float = Field(0.85, ge=0.0, le=1.0)
    trend_gate_bypass_ml_proba: float = Field(0.55, ge=0.0, le=1.0)
    pyramid_confidence_threshold: float = Field(0.85, ge=0.0, le=1.0)
    pyramid_add_fraction: float = Field(0.50, ge=0.0, le=2.0)
    orderbook_retry_enabled: bool = True
    orderbook_retry_delay_seconds: int = Field(60, ge=1, le=3600)
    orderbook_retry_attempts: int = Field(3, ge=1, le=10)

    # Exit gates (hard rules, evaluated every risk-tick)
    stop_loss_pct: float = Field(0.015, ge=0.005, le=0.20)       # 1.5% hard stop (unchanged this pass — see MAE/MFE note)
    take_profit_pct: float = Field(0.05, ge=0.005, le=0.50)      # 5% take-profit (unused by the
    # live TP1/TP2 ladder below; kept only as a display value + the
    # trailing_activation_pct getattr fallback in risk.py — do not read it
    # for exit logic, use take_profit_1_pct/take_profit_2_pct instead.
    # trailing_stop_pct (distance) widened 1.0%->2.0% (2026-08-25 optimization
    # pass, scripts/strategy_lab.py trailing sweep): a 3x3 activation x
    # distance grid on real daily OHLCV showed distance=2.0% strictly
    # outperforming 1.0%/1.25%/1.50% at EVERY activation level tested, in
    # BOTH walk-forward folds independently (not just pooled) — expectancy
    # +4.40%->+4.74%, avg_win +10.84%->+11.48%, mfe_captured 64.5%->66.6%,
    # with IDENTICAL max_drawdown/max_losing_streak. The 1.0% distance was
    # getting shaken out by ordinary volatility before winners could run.
    # trailing_activation_pct's own sweep (2.0/2.5/3.0%) showed only a
    # marginal, inconsistent edge from moving off 2.0% — left unchanged as
    # the more robust choice per "prefer ranges over one curve-fitted value".
    trailing_stop_pct: float = Field(0.02, ge=0.005, le=0.20)    # 2.0% trail from HWM
    trailing_activation_pct: float = Field(0.02, ge=0.005, le=0.50)  # arm trailing after +2%
    # TP1/TP2 scale-out ladder actually used by risk.evaluate_exits(). These
    # fields had been silently dropped from Settings (same class of gap as
    # `min_trade_usdt`/`blocked_symbols` found in the 2026-08-25 audit) while
    # risk.py kept reading them via `getattr(s, name, <default>)` — so the
    # ladder was quietly hardcoded and NOT tunable via .env despite the
    # extensive comments in risk.py implying otherwise. Restored with the
    # exact same values risk.py was already falling back to, so this is a
    # configurability fix with zero behavior change. Left UNCHANGED this
    # optimization pass per explicit instruction (first determine bad-entry
    # vs premature-exit before touching TP structure).
    take_profit_1_pct: float = Field(0.08, ge=0.005, le=0.50)     # scale out at +8%
    take_profit_1_fraction: float = Field(0.50, ge=0.05, le=1.0)  # sell 50% of the position
    take_profit_2_pct: float = Field(0.15, ge=0.005, le=1.0)      # scale out at +15%
    take_profit_2_fraction: float = Field(0.25, ge=0.05, le=1.0)  # sell 25% of the ORIGINAL stake
    max_hold_hours: int = Field(96, ge=1, le=10000)              # force-exit after 4 days
    # Tightened 0.25->0.15 (2026-08-25 optimization pass,
    # scripts/drawdown_threshold_sweep.py against 250 real live closed
    # trades): the realized max drawdown in that replay never exceeded
    # 14.34%, so thresholds >=15% NEVER would have engaged historically —
    # 0.15 provides a real protective margin just above the observed worst
    # case at ZERO historical cost (0 halts, identical trade count/return to
    # 0.25 in the replay), whereas 0.25 offered no real backstop at all since
    # it sat far above anything that ever happened.
    drawdown_circuit_breaker_pct: float = Field(0.15, ge=0.01, le=0.50)  # halt new BUYs after -15%

    # Stale / "dead money" exit — frees the slot early if a position has sat
    # for a while without meaningfully moving in our favor. Only ever forces
    # an exit; never loosens the stop-loss or widens a take-profit target.
    stale_exit_enabled: bool = True
    stale_exit_hours: int = Field(48, ge=1, le=10000)
    stale_exit_max_pnl_pct: float = Field(0.02, ge=0.0, le=0.50)

    # Entry gates
    min_signal_confidence: float = Field(0.65, ge=0.0, le=1.0)
    buy_cooldown_minutes: int = Field(20, ge=0, le=1440)

    # ProfitStream strategy controls.
    profitstream_enabled: bool = True
    profitstream_use_legacy_agents: bool = False
    profitstream_score_threshold: int = Field(80, ge=0, le=100)
    profitstream_rsi_min: int = Field(40, ge=1, le=99)
    profitstream_rsi_max: int = Field(65, ge=1, le=99)
    profitstream_volume_spike_multiple: float = Field(1.5, ge=1.0, le=10.0)
    profitstream_btc_volatility_threshold: float = Field(0.03, ge=0.001, le=0.20)
    profitstream_low_volume_quote_min: float = Field(50.0, ge=0.0, le=1_000_000.0)
    # Optional comma-separated UTC timestamps (ISO-8601) for major news events.
    # Example: "2026-07-20T12:30:00+00:00,2026-08-01T14:00:00+00:00"
    profitstream_news_events_utc: str = ""
    profitstream_news_buffer_minutes: int = Field(30, ge=0, le=240)
    # Gate on the RSI-recovery ("mean reversion exit") SELL signal: only honor
    # it when the position's unrealized PnL is at/above this fraction (0.0 =
    # breakeven). Evidence (scripts/daily_forensic_report.py against real live
    # trade history): this signal-driven exit was the single worst-performing
    # exit path (8.8% win rate over 90d, 3.2% over the 5 days right after the
    # regime-gating deploy) — RSI merely ticking back above the exit threshold
    # doesn't mean the trade recovered, and closing there regardless of price
    # pre-empts the empirically much healthier stop-loss/trailing-stop/stale-
    # exit ladder. Below this PnL, the position is left for the risk ladder to
    # manage instead of being closed at a loss by a technical-only signal.
    mean_reversion_exit_min_pnl_pct: float = Field(0.003, ge=-0.20, le=0.20)
    # RSI level considered "recovered" for the mean-reversion exit (was a bare
    # `rsi > 55` constant). Configurable so it can be validated rather than
    # hardcoded.
    mean_reversion_exit_rsi: float = Field(55.0, ge=1.0, le=99.0)
    # RSI recovery + breakeven-or-better PnL is still not sufficient on its own
    # — real trade history shows that alone produces a near-zero win rate. Also
    # require at least one independent confirmation that the move is actually
    # rolling over, using indicators the strategy already computes:
    #   - momentum confirmation: MACD histogram is declining bar-over-bar
    #     (momentum losing steam even if RSI/price still look fine).
    #   - price confirmation: close has dropped back below its own EMA20
    #     (the short-term trend itself has turned).
    # The exit requires RSI recovery + min PnL + (momentum OR price) when both
    # are enabled; if neither is enabled the exit reverts to the plain RSI+PnL
    # gate (recorded as exit_reason="mean_reversion_rsi").
    mean_reversion_exit_require_momentum_confirmation: bool = True
    mean_reversion_exit_require_price_confirmation: bool = True
    # Once a position has already run up to the trailing-stop's own activation
    # threshold (`trailing_activation_pct`), prefer letting the risk ladder
    # (TP1/TP2/trailing) manage it instead of closing early on this signal —
    # "let profitable trades develop into TP/trailing winners" rather than
    # taking a small win off the table just because RSI recovered.
    mean_reversion_exit_defer_to_risk_ladder: bool = True
    # Entry strategy A/B switch. Both share identical risk/stop/TP/trailing/
    # execution/fees — only the entry condition differs — so any performance
    # difference measured between them (paper mode / forward test) can be
    # attributed to the entry logic itself, not confounded by other changes.
    #   - "dip_buy" (current live default): RSI<30 AND close<=bb_lower.
    #   - "oversold_bounce": looser dip-buy that also requires price already
    #     off its 5-day low (not still in free-fall). Walk-forward evidence
    #     (scripts/walkforward.py --market-filter) showed a materially higher
    #     mean return than dip_buy across the folds tested, but on only 2
    #     non-empty out-of-sample folds — not enough to replace the live
    #     strategy outright. Use this switch to run it in PAPER mode alongside
    #     the live dip_buy config for a real forward A/B before considering a
    #     live swap.
    entry_strategy: str = Field("dip_buy", pattern="^(dip_buy|oversold_bounce)$")
    oversold_bounce_rsi_max: float = Field(40.0, ge=1.0, le=99.0)
    oversold_bounce_bb_multiplier: float = Field(1.02, ge=1.0, le=1.20)
    oversold_bounce_min_bounce_pct: float = Field(0.05, ge=0.0, le=0.50)
    # Long-term trend filter — only open longs when the latest daily close is
    # above its 200-EMA. Spot is long-only, so buying assets in a downtrend just
    # feeds the stop-loss gate. Backtest-validated: cuts losses ~3x and max
    # drawdown ~half versus no filter (see scripts/param_sweep diagnosis).
    trend_filter_enabled: bool = True

    # Market-regime kill-switch — block ALL new longs when the broad market
    # (BTC) is in a confirmed downtrend (50-EMA below 200-EMA, a "death cross").
    # Walk-forward evidence (scripts/walkforward.py): 100% of the strategy's
    # losses occur in sustained BTC downtrends; gating the (otherwise positive)
    # mean-reversion entries by this regime flips full-period expectancy from
    # net-negative to net-positive and caps the bear-market drawdown. Spot is
    # long-only, so there is no edge to capture while the market bleeds — sit
    # in cash instead. FAIL-OPEN: missing BTC data always allows trading.
    market_regime_gate_enabled: bool = True
    # The gate now scores BTC's regime -2..+2 (app/regime/btc_regime.py) instead
    # of a single binary EMA cross: BULL/STRONG_BULL (score>=1) allows normal
    # entries, BEAR/STRONG_BEAR (score<=-1) blocks all new longs (no bypass —
    # we have no walk-forward-validated reversal setup to justify one), and
    # SIDEWAYS (score==0) allows entries only if the strategy's own quality
    # score clears this extra bonus above `profitstream_score_threshold`.
    market_regime_sideways_score_bonus: int = Field(15, ge=0, le=100)

    # ── Anti-chase / extension guard ──────────────────────────────────
    # A dip-buy that is too far *below* its EMA20 is more likely a falling
    # knife/capitulation event than a healthy pullback — reject it instead of
    # chasing the crash. Distance is (ema20 - close) / ema20.
    max_dip_extension_pct: float = Field(0.15, ge=0.01, le=0.60)

    # ── Correlation / basket exposure cap ─────────────────────────────
    # Treat these symbols as one "basket" (they tend to move together with
    # BTC) and cap their COMBINED exposure separately from the general
    # max_long_exposure_pct, so the bot can't stack several correlated bets
    # that are effectively one trade (e.g. BTC + ETH + SOL all long at once).
    correlation_gate_enabled: bool = True
    correlated_symbol_groups: tuple[tuple[str, ...], ...] = (
        ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"),
    )
    max_correlated_exposure_pct: float = Field(0.35, ge=0.0, le=1.0)

    # ── Entry cooldown after a stop-out ────────────────────────────────
    # A plain time cooldown (buy_cooldown_minutes) applies after every BUY
    # fill; this ADDS a longer, reason-specific cooldown after a stop-loss
    # exit specifically, so the bot can't immediately re-enter the exact
    # setup that just lost money. The regular market/trend/risk gates still
    # apply on top once this cooldown clears (fresh signal + regime check).
    stop_loss_cooldown_minutes: int = Field(60, ge=0, le=1440)

    # ── Daily loss limit ───────────────────────────────────────────────
    # Distinct from drawdown_circuit_breaker_pct (which trips on cumulative
    # drawdown since the process started). This resets every UTC day and
    # halts new BUYs once today's realized losses exceed this fraction of
    # starting equity — existing positions are still protected/managed.
    daily_loss_limit_enabled: bool = True
    daily_loss_limit_pct: float = Field(0.05, ge=0.0, le=1.0)

    # Agent thresholds (tunable without code change)
    rsi_oversold: int = Field(25, ge=5, le=50)                   # was 30
    rsi_overbought: int = Field(75, ge=50, le=95)                # was 70
    breakout_lookback: int = Field(30, ge=5, le=200)             # was 20
    vol_contraction_threshold: float = Field(0.65, ge=0.1, le=1.5)  # was 0.7

    # Per-agent weights — multiplies that agent's vote in the aggregator.
    # Walk-forward evidence (scripts/walkforward.py, 25 syms / 1d / 3 folds):
    # the mean-reversion (dip-buy) logic is the ONLY component with positive
    # out-of-sample expectancy (+0.3%→+4.3% with the market filter, 2/3 folds),
    # while the trend/confluence/breakout/momentum signals are all net-negative
    # (baseline confluence ≈ -29% mean). So the aggregator now LEADS with
    # mean-reversion and demotes the trend-following voters that drive the
    # losing baseline entries. The 200-EMA trend gate + BTC market-regime gate
    # still block dip-buys during confirmed downtrends (falling-knife guard).
    agent_weight_trend_follower: float = Field(0.6, ge=0.0, le=3.0)  # demoted — net-negative out-of-sample
    agent_weight_mean_reversion: float = Field(2.0, ge=0.0, le=3.0)  # promoted — only positive-expectancy edge
    agent_weight_breakout: float = Field(0.5, ge=0.0, le=3.0)    # demoted — net negative PnL in paper
    agent_weight_momentum: float = Field(0.5, ge=0.0, le=3.0)    # demoted — too noisy
    agent_weight_volatility: float = Field(0.8, ge=0.0, le=3.0)
    agent_weight_regime_overlay: float = Field(1.5, ge=0.0, le=3.0)  # promoted — regime filter, complements market gate
    agent_weight_llm_reasoner: float = Field(1.0, ge=0.0, le=3.0)

    # Adaptive: scale each agent's weight by its rolling win-rate.
    # weight *= clamp(0.5 + win_rate, 0.5, 1.5). Disable for pure deterministic mode.
    adaptive_agent_weights: bool = True

    # ML learning loop (signal outcome labeling + retraining)
    ml_learning_enabled: bool = True
    ml_signal_horizon_minutes: int = Field(240, ge=15, le=10_080)  # default 4h
    ml_min_training_samples: int = Field(200, ge=20, le=1_000_000)
    ml_min_new_labels: int = Field(50, ge=10, le=1_000_000)
    # Slippage buffer added on top of 2x taker fee when labeling a matured
    # signal event as a win/loss (see app/regime/trainer.py _min_win_edge).
    ml_label_slippage_pct: float = Field(0.0010, ge=0.0, le=0.05)

    # ML quality gate — drop trades the learned model rates below this win-prob.
    # Closes the learning loop: realized win/loss outcomes train the model,
    # which then filters live entry signals. When enabled, uncertainty blocks
    # a new BUY rather than granting it permission.
    ml_gate_enabled: bool = True
    ml_gate_threshold: float = Field(0.50, ge=0.0, le=1.0)
    ml_gate_threshold_conf_70: float = Field(0.45, ge=0.0, le=1.0)
    ml_gate_threshold_conf_80: float = Field(0.40, ge=0.0, le=1.0)
    ml_gate_threshold_conf_90: float = Field(0.35, ge=0.0, le=1.0)
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
    # Reject entry if (ask-bid)/mid exceeds this (0.005 = 0.50%). Aligned with
    # the universe `max_spread_percent` (0.50%) — Binance.US is thin, so the old
    # 0.15% was effectively un-satisfiable and starved the bot of entries.
    max_spread_pct: float = Field(0.0025, ge=0.0, le=0.05)
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

    # Risk manager controls.
    risk_per_trade_pct: float = Field(0.01, ge=0.001, le=0.10)
    loss_streak_pause_count: int = Field(3, ge=1, le=20)
    loss_streak_pause_minutes: int = Field(60, ge=1, le=1440)

    # Health monitor / watchdog controls.
    # If `autopilot.last_tick_at` is older than this, the trading loop is stale.
    health_tick_stale_seconds: int = Field(180, ge=30, le=86_400)
    # API latency warning threshold; informational, does not auto-stop process.
    health_latency_warn_seconds: float = Field(3.0, ge=0.1, le=120.0)
    # Detect near-simultaneous duplicate order candidates (same symbol/side/mode).
    health_duplicate_order_window_seconds: int = Field(45, ge=1, le=600)
    # Count exchange-order failures over this lookback and alert/escalate at max.
    health_order_failure_lookback_minutes: int = Field(30, ge=1, le=1_440)
    health_order_failure_max: int = Field(3, ge=1, le=1_000)
    # Resource pressure warnings surfaced by watchdog.
    health_memory_rss_warn_mb: float = Field(1_024.0, ge=128.0, le=65_536.0)
    health_cpu_warn_pct: float = Field(90.0, ge=1.0, le=4_000.0)

    # Emergency-halt ladder tuning.
    emergency_halt_max_failures: int = Field(3, ge=1, le=100)
    emergency_halt_auto_clear_cycles: int = Field(3, ge=1, le=100)
    # Allow live trading to continue even after watchdog detects a stale tick
    # loop, so the bot can recover without getting perpetually blocked by a
    # transient restart/reload event. Keep the safety logger in place but do not
    # gate new entries on it unless explicitly enabled.
    emergency_halt_enabled: bool = True

    # Storage
    data_cache_dir: Path = Path("./data/cache")

    # Binance.US REST endpoint — never point this at binance.com
    binance_base_url: str = "https://api.binance.us"
    binance_ws_url: str = "wss://stream.binance.us:9443"

    @model_validator(mode="after")
    def _enforce_binance_us_only(self) -> "Settings":
        """Hard-fail startup if price/order endpoints are ever misconfigured to
        binance.com (wrong domain, wrong geofence, wrong fee schedule/symbol
        set). This is money-handling code — refuse to boot rather than trade
        against the wrong exchange."""
        import logging

        offenders = [
            ("binance_base_url", self.binance_base_url),
            ("binance_ws_url", self.binance_ws_url),
        ]
        for field_name, value in offenders:
            if "binance.com" in value.lower():
                logging.getLogger("app.config").critical(
                    "CRITICAL: %s=%r points at binance.com, not binance.us. "
                    "Refusing to start — this app must only trade on Binance.US.",
                    field_name, value,
                )
                raise ValueError(
                    f"{field_name} must point at Binance.US (api.binance.us / "
                    f"stream.binance.us), got {value!r}"
                )
        return self

    # Binance.US spot trading fees.
    # CORRECTED 2026-08-25 (forensic audit): the 0.40% tier-0 default here had
    # never matched this account's REAL negotiated rate — `client.trade_fees()`
    # against the live account returned maker=0.00%, taker=0.02%, a 20x gap
    # that had been silently overstating every historical trade's modeled
    # cost. Fees are now also read directly from each fill's actual commission
    # when available (see app/exchange/client.py `_extract_commission` /
    # `Order.commission`) — these settings are only the FALLBACK estimate used
    # when the real per-fill commission can't be determined (paper mode, a
    # missing/mixed-asset commission field). Re-verify via `client.trade_fees()`
    # if your account's tier or fee schedule changes.
    binance_maker_fee: float = Field(0.0002, ge=0.0, le=0.01)
    binance_taker_fee: float = Field(0.0002, ge=0.0, le=0.01)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so Settings is parsed once per process."""
    return Settings()

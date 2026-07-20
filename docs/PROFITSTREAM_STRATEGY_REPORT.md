# ProfitStream Strategy Upgrade Report

## Objective
This upgrade shifts the system from high-signal-volume voting to quality-first execution. The target is fewer trades, higher conviction, stronger downside control, and consistent risk posture in live mode.

## What Changed

### 1. Multi-timeframe strategy engine
- Added `app/trading/strategy.py` with a dedicated `ProfitStreamStrategy`.
- Uses:
  - `1m` for execution trigger
  - `5m` for trend/RSI and MACD-reversal exits
  - `15m` for MACD entry confirmation
  - `1h` for BTC market direction
- Why it improves results:
  - Reduces false positives by requiring alignment across fast + intermediate + regime layers.

### 2. Entry quality scoring (0-100)
- Every symbol is scored and audited each tick.
- Entry gates:
  - EMA 9 / EMA 21 bullish cross
  - RSI in 40-65 band
  - Volume spike >1.5x local average
  - MACD bullish confirmation
  - BTC higher-timeframe trend alignment
- Minimum execution score set to `>= 80`.
- Why it improves results:
  - Forces confluence instead of single-indicator entries, improving win-rate potential.

### 3. Explicit market filters
- Strategy rejects entries for:
  - High BTC volatility regime
  - Low-volume periods
  - News blackout windows (configurable UTC list, +/-30m buffer)
  - Spread > 0.25%
- Why it improves results:
  - Avoids structurally bad microstructure conditions where slippage and whipsaw dominate returns.

### 4. Exit hardening
- Kept 5% take-profit and moved stop to 1.5% (`stop_loss_pct=0.015`).
- Added `trailing_activation_pct=0.02` so trailing starts after +2%.
- Immediate SELL trigger on 5m MACD bearish reversal when a position is open.
- Why it improves results:
  - Locks gains sooner while cutting losers quickly in momentum flips.

### 5. Risk manager redesign
- Added `app/trading/risk_manager.py`.
- Enforces:
  - max 3 open positions
  - 1% portfolio risk budget per trade
  - Kelly cap + max-position cap on notional sizing
  - trading pause after 3 consecutive losses
  - automatic resume after 1 hour cooldown
- Why it improves results:
  - Controls loss clustering, prevents over-allocation, and keeps drawdown behavior bounded.

### 6. Tick-level observability and reject logging
- Added `tick_audit` migration in `app/storage/db.py` schema.
- Added storage APIs:
  - `record_tick_audit`
  - `recent_tick_audit`
- Every symbol decision stores:
  - action
  - score
  - executed flag
  - rejection reason
  - indicator payload
- Why it improves results:
  - Makes skipped-trade diagnostics measurable and repeatable, enabling fast iteration on weak filters.

### 7. Scheduler profile for live execution cadence
- Added `app/scheduler/scheduler.py` and switched scheduler export to it.
- Autopilot tick now runs every minute for faster reaction while keeping heavy jobs on slower cadence.
- Why it improves results:
  - Preserves responsiveness for exits and high-quality entries without increasing strategy looseness.

## Files Updated
- `app/trading/strategy.py`
- `app/trading/risk_manager.py`
- `app/scheduler/scheduler.py`
- `app/scheduler/__init__.py`
- `app/trading/autopilot.py`
- `app/agents/runner.py`
- `app/trading/risk.py`
- `app/ta/indicators.py`
- `app/exchange/client.py`
- `app/storage/db.py`
- `app/config.py`
- `scripts/migrate_tick_audit.py`
- `scripts/strategy_analytics.py`
- `scripts/backtest_profitstream.py`

## Expected Impact
- Better trade selection quality through confluence + score threshold.
- Lower overtrading via stricter gating and max 3 concurrent positions.
- Better drawdown containment via tighter stop, loss-streak pause, and volatility/news/spread filters.
- Improved post-trade optimization via `tick_audit` + analytics scripts.

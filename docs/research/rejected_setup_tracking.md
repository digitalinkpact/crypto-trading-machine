# Stage 1 — Rejected-setup outcome tracking (READ-ONLY)

**Status:** implemented, awaiting review before Stage 2.

## What this is

An additive, read-only evidence layer that answers one question honestly:
**for every BUY setup the bot was ready to take but rejected, would the trade
have made or lost money?**

It does not change any live behaviour. It reads the existing `tick_audit`
records, simulates the hypothetical trade from later real daily candles using
the **current** live exit ladder, and writes the results to a **separate**
research database — never a live table.

## Safety properties

- Reads `tick_audit` only (via the sanctioned storage read path). Writes nothing
  to any live table.
- Results go to a separate SQLite file (default `<data_cache_dir>/research.db`),
  physically isolated from the live `trading.db`.
- Market data is fetched read-only from the **public** Binance.US klines
  endpoint via `httpx`. No API key, no auth, no order-placement path is imported.
- Runs only when invoked by a human. Nothing is wired into the scheduler, the
  tick loop, or the risk loop.

## How a "rejected setup" is defined

`classify_rejection` keeps a `tick_audit` row only when a genuine entry setup
formed (a dip or pullback) **and** it was not taken:

- Setup readiness = `pullback_ready` true, or `decision == "buy"`, or a
  reconstructed dip-ready (RSI/BB tokens absent with `rsi_1d` present).
- Excluded: executed BUYs, already-held positions, and `insufficient_history`.

The raw reject reasons are normalized into categories (`volume`, `spread`,
`extension_guard`, `news_blackout`, `regime_gate`, `score_threshold`,
`orderbook`, `ml_gate`, `cooldown`, `position_cap`, …). A single setup counts
toward **every** filter that blocked it.

## The simulation

`simulate_exit` enters at the **next executable price** (open of the first daily
candle after the signal, plus slippage) and walks forward applying the current
live ladder, checking against candle **high/low**:

1. **Stop-loss first** — if a bar's low pierces the ATR-scaled stop (falls back
   to `stop_loss_pct`), it resolves before any target in that bar.
2. **TP1 / TP2** partial scale-outs on the up-move.
3. **Trailing stop** on the remainder (armed from prior-bar HWM to avoid
   intrabar look-ahead).
4. **Stale "dead money"** then **max-hold** at bar close.

Real taker fees (`binance_taker_fee`) are charged on entry and every exit leg,
plus a per-fill slippage estimate (default `0.0010`, matching the ML labeler).
Positions that never hit a terminal exit within available candles are marked
`unresolved` (right-censored) and reported separately.

Each stored row records: symbol, timestamp, reject reason(s), BTC regime label,
entry type, score, simulated PnL %, exit reason, MFE, MAE, and hold time.

## Usage

```bash
# 1. Build the outcome data (read-only; fetches public candles).
python -m scripts.simulate_rejected_setups --mode live

#    Bound the network / scope while exploring:
python -m scripts.simulate_rejected_setups --mode live --max-symbols 40 --slippage 0.0015

# 2. Report, grouped by reject reason (or regime / exit / entry).
python -m scripts.rejected_setup_report
python -m scripts.rejected_setup_report --by regime
python -m scripts.rejected_setup_report --by exit
```

## Reading the report

- A group with **high win% / positive expectancy** is a filter that is
  **blocking winners** (a cost of that filter).
- A group with **low win% / negative expectancy** is a filter that is
  **saving losses** (a benefit of that filter).
- Each group shows counts, win rate, expectancy, a bootstrap 95% CI on
  expectancy, and profit factor.
- Groups with **fewer than 30 samples** are flagged `INSUFFICIENT` — not yet
  conclusive. Do not act on them.

This is analysis only. It never changes live gating; a human decides whether any
finding warrants a change.

## Files

- `app/research/rejected_setups.py` — classification, simulation, storage.
- `app/research/bootstrap.py` — bootstrap CI + expectancy/profit-factor helpers.
- `scripts/simulate_rejected_setups.py` — builder CLI.
- `scripts/rejected_setup_report.py` — report CLI.
- `tests/test_rejected_setups.py` — unit tests (offline, synthetic candles).

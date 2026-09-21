# Promotion criteria — ProfitStream live strategy

**Written before viewing any Stage 2 statistics, on purpose.** These thresholds
are committed up front so the decision cannot be rationalized after seeing the
numbers. `app/trading/experiment_stats.py` evaluates live `closed_trades`
against exactly these rules and prints a verdict. **The script never changes
live config — a human applies any change.**

All statistics are computed from realized live `closed_trades`, whose PnL
already reflects actual fills (fees and slippage are baked in); fee-corrected
columns (`pnl_corrected` / `pnl_pct_corrected`) are preferred when present.

## Criteria (all must pass to PROMOTE)

1. **Sample size** — at least **30** closed trades.
2. **Regime coverage** — at least **one BTC regime change** observed across the
   trade history (trades occurred under at least two distinct BTC regime labels,
   derived from each trade's `entry_btc_regime` score). A strategy only ever
   tested in one regime is unproven.
3. **Positive edge after costs** — the **lower bound of the bootstrap 95%
   confidence interval on per-trade expectancy (pnl %)** is **above zero**.
   Reporting the range (not a single point) guards against a lucky mean.
4. **Profit factor** — gross profit / gross loss is **above 1.5**.
5. **No concentration** — **no single trade** and **no single symbol** accounts
   for **more than 40%** of total gross profit. This rejects an edge that is
   really one or two lucky trades.

## Verdict logic

- **PROMOTE** — every criterion above passes.
- **REJECT** — there is adequate data (≥ 30 trades) *and* the evidence shows no
  edge: the expectancy CI **upper** bound is at or below zero, **or** profit
  factor is below 1.0. Adequate data + confidently unprofitable = stop.
- **KEEP TESTING** — anything else: too few trades, no observed regime change,
  a positive-but-unproven mean whose CI still straddles zero, profit factor
  between 1.0 and 1.5, or excessive concentration. Not enough evidence to
  promote, not enough to reject.

## What promotion would mean (human action only)

"PROMOTE" is a signal for a human to consider a change (e.g. enabling a broader
universe, raising sizing, or graduating a paper-tested variant to live) — it is
**not** an instruction and nothing is applied automatically. "REJECT" is a
signal to stop testing the current configuration as-is. "KEEP TESTING" means
gather more forward evidence before deciding.

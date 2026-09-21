"""Walk-forward strategy comparison — honest out-of-sample evaluation.

Unlike scripts/param_sweep (single in-sample window, one fixed signal), this
fetches a long history per symbol, splits it into consecutive time folds, and
runs SEVERAL candidate signal logics through the same vectorbt engine on EACH
fold. A strategy only earns trust if it is positive across folds AND across a
majority of symbols — that is what separates real edge from an overfit window.

No parameters are fitted to the data here: each strategy uses fixed, theory-
driven rules, so every fold is effectively out-of-sample for the logic.

    python -m scripts.walkforward --timeframe 1d --folds 3

Read-only: touches no config, places no orders, only pulls public klines.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Callable

import numpy as np
import pandas as pd

from app.backtest.vbt import run_vectorbt_backtest
from app.config import SYMBOLS, Timeframe, get_settings
from app.data.ohlcv import OHLCVRepository
from app.ta.indicators import add_indicators

Signals = tuple[pd.Series, pd.Series]
Strategy = Callable[[pd.DataFrame], Signals]


# ── Candidate strategies ────────────────────────────────────────────────
# Each returns (entries, exits) boolean Series aligned to df.index. Stops/TPs
# are applied uniformly by the backtest engine, so these encode DIRECTION only.

def _cross_up(cond: pd.Series) -> pd.Series:
    """True only on the bar a condition flips False->True (no re-entry spam)."""
    return cond & ~cond.shift(1, fill_value=False)


def s_baseline(df: pd.DataFrame) -> Signals:
    """Current production-style 3-of-4 bull/bear confluence, NO trend filter."""
    close, ema20, ema50 = df["close"], df["ema_20"], df["ema_50"]
    rsi, mh, bbm = df["rsi_14"], df["macd_hist"], df["bb_mid"]
    bull = ((ema20 > ema50).astype(int) + (mh > 0).astype(int)
            + ((rsi > 30) & (rsi < 70)).astype(int) + (close > bbm).astype(int))
    bear = ((ema20 < ema50).astype(int) + (mh < 0).astype(int)
            + ((rsi >= 70) | (rsi <= 30)).astype(int) + (close < bbm).astype(int))
    return _cross_up(bull >= 3).fillna(False), _cross_up(bear >= 3).fillna(False)


def s_dip_buy(df: pd.DataFrame) -> Signals:
    """The mean_reversion agent in isolation: buy oversold below lower BB.

    Included to MEASURE the falling-knife hypothesis — this is the highest-
    confidence live BUY agent (0.75) and the suspected source of the bleed.
    """
    close, rsi = df["close"], df["rsi_14"]
    entries = (rsi < 30) & (close <= df["bb_lower"])
    exits = rsi > 55  # revert toward the mean
    return _cross_up(entries).fillna(False), exits.fillna(False)


def s_trend_follow(df: pd.DataFrame) -> Signals:
    """Long-only trend following: enter when EMA20>EMA50 AND price>EMA200."""
    close, ema20, ema50, ema200 = df["close"], df["ema_20"], df["ema_50"], df["ema_200"]
    long = (ema20 > ema50) & (close > ema200)
    exits = ema20 < ema50
    return _cross_up(long).fillna(False), exits.fillna(False)


def s_trend_confluence(df: pd.DataFrame) -> Signals:
    """Baseline confluence GATED by the 200-EMA trend (longs only in uptrends)."""
    entries, exits = s_baseline(df)
    entries = entries & (df["close"] >= df["ema_200"])
    return entries.fillna(False), exits.fillna(False)


def s_donchian_trend(df: pd.DataFrame) -> Signals:
    """Classic crypto edge: Donchian-channel breakout filtered by EMA200.

    Enter on a 20-bar high break while above the 200-EMA; exit on a 10-bar low.
    """
    close, ema200 = df["close"], df["ema_200"]
    hi = df["high"].rolling(20).max().shift(1)
    lo = df["low"].rolling(10).min().shift(1)
    entries = (close > hi) & (close > ema200)
    exits = close < lo
    return _cross_up(entries).fillna(False), exits.fillna(False)


STRATEGIES: dict[str, Strategy] = {
    "baseline":         s_baseline,
    "dip_buy":          s_dip_buy,
    "trend_follow":     s_trend_follow,
    "trend_confluence": s_trend_confluence,
    "donchian_trend":   s_donchian_trend,
}


# ── Data ────────────────────────────────────────────────────────────────
async def _load(repo: OHLCVRepository, tf: Timeframe, bars: int) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        try:
            df = await repo.get(sym, tf, limit=bars, refresh=True)
        except Exception:
            continue
        if df is None or len(df) < 220:
            continue
        df = add_indicators(df).dropna()
        if len(df) >= 120:
            out[sym] = df
    return out


def _fold_bounds(n: int, folds: int) -> list[tuple[int, int]]:
    edges = np.linspace(0, n, folds + 1, dtype=int)
    return [(edges[i], edges[i + 1]) for i in range(folds)]


def _eval_fold(
    frames: dict[str, pd.DataFrame], strat: Strategy, fold: int, folds: int,
    sl: float, tp: float, fees: float, market: pd.Series | None = None,
) -> dict[str, float]:
    rets: list[float] = []
    sharpes: list[float] = []
    trades = 0
    for df in frames.values():
        lo, hi = _fold_bounds(len(df), folds)[fold]
        sub = df.iloc[lo:hi]
        if len(sub) < 40:
            continue
        entries, exits = strat(sub)
        if market is not None:
            # Risk-on mask: only allow entries when the broad market (BTC) is
            # in an uptrend. ffill aligns BTC's calendar onto this symbol's.
            risk_on = market.reindex(sub.index, method="ffill").fillna(False)
            entries = entries & risk_on.astype(bool)
        if not bool(entries.any()):
            continue
        stats = run_vectorbt_backtest(
            df=sub, entries=entries, exits=exits,
            init_cash=10_000.0, fees=fees, sl_stop=sl, tp_stop=tp,
        )
        rets.append(stats["total_return"])
        sharpes.append(stats["sharpe"])
        trades += stats["trades"]
    if not rets:
        return {}
    arr = np.array(rets)
    return {
        "ret": float(arr.mean()),
        "median": float(np.median(arr)),
        "pos_frac": float((arr > 0).mean()),
        "sharpe": float(np.mean(sharpes)),
        "trades": trades,
        "symbols": len(rets),
    }


async def main_async(
    tf: Timeframe, folds: int, sl: float, tp: float, bars: int, market_filter: bool
) -> None:
    fees = get_settings().binance_taker_fee
    repo = OHLCVRepository()
    print(f"Fetching up to {bars} {tf.value} bars per symbol...")
    frames = await _load(repo, tf, bars)
    if not frames:
        print("No usable symbols — not enough history.")
        return
    market: pd.Series | None = None
    if market_filter:
        btc = frames.get("BTCUSDT")
        if btc is None:
            print("market filter requested but BTCUSDT unavailable — skipping it.")
        else:
            # Risk-on = BTC in a confirmed uptrend (50-EMA above 200-EMA). This
            # "golden/death cross" regime is far smoother than price>EMA200, so
            # it stays risk-OFF through an entire bear instead of whipsawing on
            # every dead-cat bounce above the slow average.
            market = (btc["ema_50"] > btc["ema_200"])
    span = min(df.index.min() for df in frames.values()), max(df.index.max() for df in frames.values())
    print(f"loaded {len(frames)} symbols on {tf.value} | exits: signal+SL {sl:.0%}/TP {tp:.0%} "
          f"| fees {fees:.2%}/side | market_filter={'BTC 50>200 EMA' if market is not None else 'off'}")
    print(f"window ~ {span[0].date()} → {span[1].date()}, {folds} folds\n")

    print(f"{'strategy':>17} {'fold':>5} {'ret':>9} {'median':>9} {'pos%':>6} "
          f"{'sharpe':>8} {'trades':>7} {'syms':>5}")
    print("-" * 76)
    summary: dict[str, list[float]] = {}
    for name, strat in STRATEGIES.items():
        fold_rets: list[float] = []
        for f in range(folds):
            m = _eval_fold(frames, strat, f, folds, sl, tp, fees, market)
            if not m:
                print(f"{name:>17} {f:>5} {'(no trades)':>9}")
                continue
            fold_rets.append(m["ret"])
            print(f"{name:>17} {f:>5} {m['ret']:>+9.2%} {m['median']:>+9.2%} "
                  f"{m['pos_frac']:>+6.0%} {m['sharpe']:>+8.1%} {m['trades']:>7} {m['symbols']:>5}")
        summary[name] = fold_rets
        print()

    # Robustness verdict: positive mean AND positive in every fold.
    print("=" * 76)
    print(f"{'strategy':>17} {'mean_ret':>10} {'worst_fold':>11} {'folds_+':>8}  verdict")
    print("-" * 76)
    for name, fr in summary.items():
        if not fr:
            print(f"{name:>17} {'n/a':>10}")
            continue
        mean_r = float(np.mean(fr))
        worst = float(min(fr))
        pos_folds = sum(1 for r in fr if r > 0)
        robust = mean_r > 0 and pos_folds == len(fr)
        verdict = "ROBUST +" if robust else ("mixed" if mean_r > 0 else "negative")
        print(f"{name:>17} {mean_r:>+10.2%} {worst:>+11.2%} {pos_folds:>4}/{len(fr):<3}  {verdict}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--timeframe", default=Timeframe.D1.value,
                   choices=[tf.value for tf in Timeframe])
    p.add_argument("--folds", type=int, default=3)
    p.add_argument("--sl", type=float, default=0.08, help="stop-loss fraction")
    p.add_argument("--tp", type=float, default=0.25, help="take-profit fraction")
    p.add_argument("--bars", type=int, default=1000, help="bars to fetch per symbol")
    p.add_argument("--market-filter", action="store_true",
                   help="only allow entries when BTC > its 200-EMA (risk-on)")
    args = p.parse_args()
    asyncio.run(main_async(Timeframe(args.timeframe), args.folds, args.sl, args.tp,
                           args.bars, args.market_filter))


if __name__ == "__main__":
    main()

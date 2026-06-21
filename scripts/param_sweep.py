"""Temporary parameter sweep over the cached universe.

Sweeps stop-loss / take-profit / RSI bands through the same vectorbt replay the
production backtest uses, and reports aggregate return / win-rate / Sharpe /
drawdown per combo so we can pick a balanced tune. Read-only: touches no config.

    python -m scripts.param_sweep --timeframe 1d
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.backtest.vbt import run_vectorbt_backtest
from app.config import SYMBOLS, Timeframe, get_settings
from app.data.ohlcv import OHLCVRepository
from app.ta.indicators import add_indicators


@dataclass
class Combo:
    sl: float
    tp: float
    rsi_lo: int
    rsi_hi: int
    trend: bool = False


def _build_signals(
    df: pd.DataFrame, rsi_lo: int, rsi_hi: int, trend: bool = False
) -> tuple[pd.Series, pd.Series]:
    close = df["close"]
    ema20, ema50 = df["ema_20"], df["ema_50"]
    rsi = df["rsi_14"]
    macd_hist = df["macd_hist"]
    bb_mid = df["bb_mid"]

    bull = (
        (ema20 > ema50).astype(int)
        + (macd_hist > 0).astype(int)
        + ((rsi > rsi_lo) & (rsi < rsi_hi)).astype(int)
        + (close > bb_mid).astype(int)
    )
    bear = (
        (ema20 < ema50).astype(int)
        + (macd_hist < 0).astype(int)
        + ((rsi >= rsi_hi) | (rsi <= rsi_lo)).astype(int)
        + (close < bb_mid).astype(int)
    )
    entries = (bull >= 3) & (bull.shift(1) < 3)
    exits = (bear >= 3) & (bear.shift(1) < 3)
    # Long-term trend filter — mirror the live autopilot `_trend_gate`: only
    # take longs when price is at/above its 200-EMA. Spot is long-only, so a
    # downtrend long just feeds the stop-loss. This is the hypothesis under
    # test: does the trend gate the live system already runs flip expectancy?
    if trend and "ema_200" in df.columns:
        entries = entries & (close >= df["ema_200"])
    return entries.fillna(False), exits.fillna(False)


async def _load(repo: OHLCVRepository, tf: Timeframe) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        try:
            df = await repo.get(sym, tf, limit=500, refresh=False)
        except Exception:
            continue
        if df is None or len(df) < 100:
            continue
        df = add_indicators(df).dropna()
        if len(df) >= 50:
            out[sym] = df
    return out


def _eval(frames: dict[str, pd.DataFrame], combo: Combo, fees: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sym, df in frames.items():
        entries, exits = _build_signals(df, combo.rsi_lo, combo.rsi_hi, combo.trend)
        if not entries.any():
            continue
        stats = run_vectorbt_backtest(
            df=df, entries=entries, exits=exits,
            init_cash=10_000.0, fees=fees, sl_stop=combo.sl, tp_stop=combo.tp,
        )
        rows.append(stats)
    if not rows:
        return {}
    d = pd.DataFrame(rows)
    return {
        "ret": d["total_return"].mean(),
        "sharpe": d["sharpe"].mean(),
        "dd": d["max_drawdown"].mean(),
        "win": d["win_rate"].mean(),
        "trades": int(d["trades"].sum()),
        "pos_frac": float((d["total_return"] > 0).mean()),
    }


async def main_async(tf: Timeframe) -> None:
    settings = get_settings()
    fees = settings.binance_taker_fee
    repo = OHLCVRepository()
    frames = await _load(repo, tf)
    print(f"loaded {len(frames)} symbols on {tf.value}\n")

    sls = [0.02, 0.04, 0.06, 0.08, 0.10]
    tps = [0.05, 0.08, 0.12, 0.18, 0.25]
    rsi_bands = [(25, 75), (30, 70), (35, 80)]

    results: list[tuple[Combo, dict[str, Any]]] = []
    for trend in (False, True):
        for rlo, rhi in rsi_bands:
            for sl in sls:
                for tp in tps:
                    if tp <= sl:
                        continue
                    combo = Combo(sl=sl, tp=tp, rsi_lo=rlo, rsi_hi=rhi, trend=trend)
                    m = _eval(frames, combo, fees)
                    if m:
                        results.append((combo, m))

    # Balanced score: reward avg return + share of profitable symbols + sharpe,
    # penalize drawdown. Tuned to avoid degenerate low-trade combos.
    def score(m: dict[str, Any]) -> float:
        return m["ret"] * 2.0 + m["pos_frac"] * 0.5 + m["sharpe"] * 0.002 - m["dd"] * 0.5

    results.sort(key=lambda r: score(r[1]), reverse=True)

    print(f"{'trend':>6} {'sl':>5} {'tp':>5} {'rsi':>8} | {'ret':>8} {'win':>7} {'pos%':>6} {'sharpe':>8} {'maxdd':>8} {'trades':>6} {'score':>8}")
    print("-" * 92)
    for combo, m in results[:15]:
        print(
            f"{'ON' if combo.trend else 'off':>6} "
            f"{combo.sl:>5.0%} {combo.tp:>5.0%} {combo.rsi_lo:>3}/{combo.rsi_hi:<3} | "
            f"{m['ret']:>+8.2%} {m['win']:>+7.1%} {m['pos_frac']:>+6.0%} "
            f"{m['sharpe']:>+8.1%} {m['dd']:>+8.2%} {m['trades']:>6} {score(m):>+8.3f}"
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--timeframe", default=Timeframe.D1.value,
                   choices=[tf.value for tf in Timeframe])
    args = p.parse_args()
    asyncio.run(main_async(Timeframe(args.timeframe)))


if __name__ == "__main__":
    main()

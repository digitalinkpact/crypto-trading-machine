"""Vectorbt replay over the cached OHLCV universe.

Builds a simple consolidated entry/exit signal from the indicator stack and
applies the live risk gates (`sl_stop`, `tp_stop`) so the backtest reflects
the same exits the production loop enforces.

Usage:
    python -m scripts.backtest_strategy
    python -m scripts.backtest_strategy --timeframe 1h --refresh
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pandas as pd

from app.backtest.vbt import run_vectorbt_backtest
from app.config import SYMBOLS, Timeframe, get_settings
from app.data.ohlcv import OHLCVRepository
from app.ta.indicators import add_indicators


def _build_signals(df: pd.DataFrame, settings) -> tuple[pd.Series, pd.Series]:
    """Vectorized approximation of the production aggregator.

    Bull score adds 1 for each of: EMA20>EMA50, MACD hist>0, RSI between
    rsi_oversold..rsi_overbought, close>BB mid. Bear score is the mirror.
    Entry on bull>=3 cross from below; exit on bear>=3.
    """
    close = df["close"]
    ema20, ema50 = df["ema_20"], df["ema_50"]
    rsi = df["rsi_14"]
    macd_hist = df["macd_hist"]
    bb_mid = df["bb_mid"]

    bull = (
        (ema20 > ema50).astype(int)
        + (macd_hist > 0).astype(int)
        + ((rsi > settings.rsi_oversold) & (rsi < settings.rsi_overbought)).astype(int)
        + (close > bb_mid).astype(int)
    )
    bear = (
        (ema20 < ema50).astype(int)
        + (macd_hist < 0).astype(int)
        + ((rsi >= settings.rsi_overbought) | (rsi <= settings.rsi_oversold)).astype(int)
        + (close < bb_mid).astype(int)
    )

    entries = (bull >= 3) & (bull.shift(1) < 3)
    exits = (bear >= 3) & (bear.shift(1) < 3)
    return entries.fillna(False), exits.fillna(False)


async def _run_one(
    repo: OHLCVRepository,
    symbol: str,
    timeframe: Timeframe,
    refresh: bool,
    settings,
) -> dict[str, Any] | None:
    try:
        df = await repo.get(symbol, timeframe, limit=500, refresh=refresh)
    except Exception as exc:  # pragma: no cover - network
        return {"symbol": symbol, "error": str(exc)}
    if df is None or len(df) < 100:
        return {"symbol": symbol, "error": "insufficient candles"}

    df = add_indicators(df).dropna()
    if len(df) < 50:
        return {"symbol": symbol, "error": "insufficient post-indicator rows"}

    entries, exits = _build_signals(df, settings)
    if not entries.any():
        return {"symbol": symbol, "error": "no entries"}

    stats = run_vectorbt_backtest(
        df=df,
        entries=entries,
        exits=exits,
        init_cash=10_000.0,
        fees=0.001,
        sl_stop=settings.stop_loss_pct,
        tp_stop=settings.take_profit_pct,
    )
    stats["symbol"] = symbol
    return stats


async def main_async(timeframe: Timeframe, refresh: bool, symbols: tuple[str, ...]) -> None:
    settings = get_settings()
    repo = OHLCVRepository()

    rows: list[dict[str, Any]] = []
    for sym in symbols:
        result = await _run_one(repo, sym, timeframe, refresh, settings)
        if result:
            rows.append(result)

    ok = [r for r in rows if "error" not in r]
    bad = [r for r in rows if "error" in r]

    if ok:
        df = pd.DataFrame(ok).set_index("symbol")
        df = df[["total_return", "sharpe", "max_drawdown", "win_rate", "trades"]]
        df = df.sort_values("sharpe", ascending=False)
        with pd.option_context("display.float_format", "{:+.2%}".format):
            print(df.to_string())

        print("\n── aggregate ──")
        print(f"symbols tested:   {len(ok)}")
        print(f"avg total_return: {df['total_return'].mean():+.2%}")
        print(f"avg sharpe:       {df['sharpe'].mean():+.2f}")
        print(f"avg max_dd:       {df['max_drawdown'].mean():+.2%}")
        print(f"avg win_rate:     {df['win_rate'].mean():+.2%}")
        print(f"total trades:     {int(df['trades'].sum())}")

    if bad:
        print("\n── skipped ──")
        for r in bad:
            print(f"  {r['symbol']:<10} {r['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the strategy across the universe.")
    parser.add_argument(
        "--timeframe", default=Timeframe.D1.value,
        choices=[tf.value for tf in Timeframe],
        help="Candle timeframe (default: 1d).",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Pull fresh candles from Binance.US (default: use cache only).",
    )
    parser.add_argument(
        "--symbol", action="append", default=None,
        help="Restrict to one symbol (can repeat). Defaults to full universe.",
    )
    args = parser.parse_args()

    tf = Timeframe(args.timeframe)
    syms = tuple(args.symbol) if args.symbol else SYMBOLS
    asyncio.run(main_async(tf, args.refresh, syms))


if __name__ == "__main__":
    main()

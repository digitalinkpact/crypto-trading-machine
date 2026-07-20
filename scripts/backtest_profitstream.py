"""Backtest ProfitStream rules on BTC/ETH/SOL for the last 90 days.

Usage:
    python -m scripts.backtest_profitstream
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from math import sqrt
from statistics import mean, pstdev

import pandas as pd

from app.backtest.vbt import _import_vectorbt_compat
from app.config import get_settings
from app.exchange import BinanceUSClient
from app.ta import add_indicators

vbt = _import_vectorbt_compat()

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


async def _fetch_90d_5m(symbol: str) -> pd.DataFrame:
    client = BinanceUSClient()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)

    rows = []
    # Binance.US public klines pagination.
    t = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while t < end_ms:
        chunk = await asyncio.to_thread(
            client._spot.klines,
            symbol,
            "5m",
            limit=1000,
            startTime=t,
            endTime=end_ms,
        )
        if not chunk:
            break
        rows.extend(chunk)
        last_open = int(chunk[-1][0])
        t = last_open + 5 * 60 * 1000
        if len(chunk) < 1000:
            break

    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base_volume", "taker_quote_volume", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.set_index("close_time")


def _signals(df: pd.DataFrame, btc_1h: pd.Series) -> tuple[pd.Series, pd.Series]:
    x = add_indicators(df).copy()
    x["ema9"] = x["close"].ewm(span=9, adjust=False).mean()
    x["ema21"] = x["close"].ewm(span=21, adjust=False).mean()
    x["vol_ma20"] = x["volume"].rolling(20).mean()
    x["quote"] = x["close"] * x["volume"]

    ema_cross = (x["ema9"] > x["ema21"]) & (x["ema9"].shift(1) <= x["ema21"].shift(1))
    rsi_ok = (x["rsi_14"] >= 40) & (x["rsi_14"] <= 65)
    vol_spike = x["volume"] > (1.5 * x["vol_ma20"])
    macd_bull = x["macd"] > x["macd_signal"]
    btc_ok = btc_1h.reindex(x.index, method="ffill").fillna(False)

    score = (
        ema_cross.astype(int) * 25
        + rsi_ok.astype(int) * 15
        + vol_spike.astype(int) * 15
        + macd_bull.astype(int) * 20
        + btc_ok.astype(int) * 15
        + ((x["quote"] > 50).astype(int)) * 10
    )

    entries = (score >= 80).reindex(df.index).fillna(False)
    macd_reversal = (x["macd"] < x["macd_signal"]) & (x["macd"].shift(1) >= x["macd_signal"].shift(1))
    exits = macd_reversal.reindex(df.index).fillna(False)
    return entries.fillna(False), exits.fillna(False)


async def main() -> None:
    s = get_settings()
    out_rows = []

    btc_df = await _fetch_90d_5m("BTCUSDT")
    btc_1h = btc_df["close"].resample("1h").last().dropna()
    btc_ema20 = btc_1h.ewm(span=20, adjust=False).mean()
    btc_ema50 = btc_1h.ewm(span=50, adjust=False).mean()
    btc_trend = (btc_1h > btc_ema50) & (btc_ema20 > btc_ema50)

    for symbol in SYMBOLS:
        df = await _fetch_90d_5m(symbol)
        if df.empty or len(df) < 500:
            print(f"{symbol}: insufficient data")
            continue
        entries, exits = _signals(df, btc_trend)
        pf = vbt.Portfolio.from_signals(
            close=df["close"],
            entries=entries,
            exits=exits,
            init_cash=10_000.0,
            fees=s.binance_taker_fee,
            sl_stop=s.stop_loss_pct,
            tp_stop=s.take_profit_pct,
            freq="5min",
        )
        stats = pf.stats()
        trade_rets = pf.trades.records_readable.get("Return [%]", pd.Series(dtype=float))
        mu = float(trade_rets.mean() / 100.0) if len(trade_rets) else 0.0
        sigma = float(pstdev((trade_rets / 100.0).tolist())) if len(trade_rets) > 1 else 0.0
        sharpe = (mu / sigma * sqrt(252)) if sigma > 0 else float(stats.get("Sharpe Ratio", 0.0))

        out_rows.append(
            {
                "symbol": symbol,
                "win_rate": float(stats.get("Win Rate [%]", 0.0)) / 100.0,
                "profit_factor": float(stats.get("Profit Factor", 0.0)),
                "sharpe": float(sharpe),
                "max_drawdown": float(stats.get("Max Drawdown [%]", 0.0)) / 100.0,
                "avg_hold_hours": float(stats.get("Avg Trade Duration", pd.Timedelta(0)).total_seconds() / 3600.0)
                if hasattr(stats.get("Avg Trade Duration", None), "total_seconds")
                else 0.0,
                "trades": int(stats.get("Total Trades", 0)),
                "total_return": float(stats.get("Total Return [%]", 0.0)) / 100.0,
            }
        )

    if not out_rows:
        print("No backtest results")
        return

    out = pd.DataFrame(out_rows).set_index("symbol")
    print(out.to_string(float_format=lambda x: f"{x:.4f}"))

    print("\n=== Aggregate (90d) ===")
    print(f"win_rate={out['win_rate'].mean():.2%}")
    print(f"profit_factor={out['profit_factor'].mean():.2f}")
    print(f"sharpe={out['sharpe'].mean():.2f}")
    print(f"max_drawdown={out['max_drawdown'].mean():.2%}")
    print(f"avg_hold_hours={out['avg_hold_hours'].mean():.2f}")


if __name__ == "__main__":
    asyncio.run(main())

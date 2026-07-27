"""ProfitStream backtest suite (90d, 180d, Monte Carlo, walk-forward).

Usage:
    python -m scripts.profitstream_backtest_suite
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from statistics import mean

import numpy as np
import pandas as pd

from app.backtest.vbt import _import_vectorbt_compat
from app.config import Timeframe, get_settings
from app.data.ohlcv import OHLCVRepository
from app.ta import add_indicators

vbt = _import_vectorbt_compat()

SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT",
    "AVAXUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT",
)


@dataclass
class BacktestResult:
    window_days: int
    symbol: str
    total_return: float
    win_rate: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    trades: int


def _build_signals(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    s = get_settings()
    x = add_indicators(df).dropna().copy()
    if x.empty:
        idx = df.index
        return pd.Series(False, index=idx), pd.Series(False, index=idx)

    score = (
        ((x["ema_20"] > x["ema_50"]).astype(int) * 20)
        + ((x["macd"] > x["macd_signal"]).astype(int) * 20)
        + (((x["rsi_14"] >= s.profitstream_rsi_min) & (x["rsi_14"] <= s.profitstream_rsi_max)).astype(int) * 20)
        + ((x["close"] > x["ema_200"]).astype(int) * 20)
        + ((x["volume"] > (x["volume"].rolling(20).mean() * s.profitstream_volume_spike_multiple)).fillna(False).astype(int) * 20)
    )

    entries = (score >= s.profitstream_score_threshold) & (score.shift(1).fillna(0) < s.profitstream_score_threshold)
    exits = ((x["macd"] < x["macd_signal"]) | (x["close"] < x["ema_50"]))
    return entries.reindex(df.index, fill_value=False), exits.reindex(df.index, fill_value=False)


def _run_pf(df: pd.DataFrame, entries: pd.Series, exits: pd.Series):
    s = get_settings()
    pf = vbt.Portfolio.from_signals(
        close=df["close"],
        entries=entries,
        exits=exits,
        init_cash=1000.0,
        fees=s.binance_taker_fee,
        sl_stop=s.stop_loss_pct,
        tp_stop=s.final_take_profit_pct,
        freq="1d",
    )
    stats = pf.stats()
    trades = pf.trades.records_readable
    profits = trades.get("PnL", pd.Series(dtype=float))
    gross_profit = float(profits[profits > 0].sum()) if len(profits) else 0.0
    gross_loss = abs(float(profits[profits < 0].sum())) if len(profits) else 0.0
    pfactor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    return {
        "stats": stats,
        "trades": trades,
        "profit_factor": pfactor,
    }


async def _fetch_window(symbol: str, days: int) -> pd.DataFrame:
    repo = OHLCVRepository()
    bars = max(days + 60, 260)
    df = await repo.get(symbol, Timeframe.D1, limit=bars, refresh=True)
    if df is None or df.empty:
        return pd.DataFrame()
    return df.tail(days + 5).copy()


async def run_window(days: int) -> list[BacktestResult]:
    out: list[BacktestResult] = []
    for symbol in SYMBOLS:
        df = await _fetch_window(symbol, days)
        if df.empty or len(df) < 80:
            continue
        entries, exits = _build_signals(df)
        result = _run_pf(df, entries, exits)
        stats = result["stats"]
        out.append(
            BacktestResult(
                window_days=days,
                symbol=symbol,
                total_return=float(stats.get("Total Return [%]", 0.0)) / 100.0,
                win_rate=float(stats.get("Win Rate [%]", 0.0)) / 100.0,
                profit_factor=float(result["profit_factor"]),
                sharpe=float(stats.get("Sharpe Ratio", 0.0)),
                max_drawdown=float(stats.get("Max Drawdown [%]", 0.0)) / 100.0,
                trades=int(stats.get("Total Trades", 0)),
            )
        )
    return out


def _summarize(results: list[BacktestResult], label: str) -> dict:
    if not results:
        return {"label": label, "symbols": 0}
    return {
        "label": label,
        "symbols": len(results),
        "avg_return": mean(r.total_return for r in results),
        "avg_win_rate": mean(r.win_rate for r in results),
        "avg_profit_factor": mean(r.profit_factor for r in results),
        "avg_sharpe": mean(r.sharpe for r in results),
        "avg_max_drawdown": mean(r.max_drawdown for r in results),
        "total_trades": sum(r.trades for r in results),
    }


def _extract_trade_returns(results: list[BacktestResult]) -> list[float]:
    # Use symbol-level returns as conservative distribution if detailed records vary by symbol.
    return [r.total_return for r in results if not np.isnan(r.total_return)]


def monte_carlo(returns: list[float], trials: int = 2000, horizon_trades: int = 60) -> dict:
    if not returns:
        return {"trials": trials, "horizon_trades": horizon_trades, "p5": 0.0, "p50": 0.0, "p95": 0.0}
    sims = []
    for _ in range(trials):
        equity = 1.0
        for _ in range(horizon_trades):
            r = random.choice(returns)
            equity *= (1.0 + r)
        sims.append(equity - 1.0)
    sims.sort()
    return {
        "trials": trials,
        "horizon_trades": horizon_trades,
        "p5": sims[int(0.05 * len(sims))],
        "p50": sims[int(0.50 * len(sims))],
        "p95": sims[int(0.95 * len(sims))],
    }


async def walk_forward(days: int = 180, folds: int = 3) -> dict:
    fold_returns: list[float] = []
    repo = OHLCVRepository()
    for symbol in SYMBOLS:
        df = await repo.get(symbol, Timeframe.D1, limit=days + 80, refresh=True)
        if df is None or len(df) < (folds * 40):
            continue
        df = df.tail(days).copy()
        edges = np.linspace(0, len(df), folds + 1, dtype=int)
        for i in range(folds):
            sub = df.iloc[edges[i]:edges[i + 1]].copy()
            if len(sub) < 40:
                continue
            entries, exits = _build_signals(sub)
            pf = _run_pf(sub, entries, exits)
            fold_returns.append(float(pf["stats"].get("Total Return [%]", 0.0)) / 100.0)
    if not fold_returns:
        return {"folds": folds, "avg_return": 0.0, "positive_fold_ratio": 0.0, "worst_fold": 0.0}
    return {
        "folds": folds,
        "avg_return": float(mean(fold_returns)),
        "positive_fold_ratio": float(sum(1 for r in fold_returns if r > 0) / len(fold_returns)),
        "worst_fold": float(min(fold_returns)),
        "best_fold": float(max(fold_returns)),
    }


async def main() -> None:
    r90 = await run_window(90)
    r180 = await run_window(180)

    s90 = _summarize(r90, "90d")
    s180 = _summarize(r180, "180d")
    mc = monte_carlo(_extract_trade_returns(r180), trials=2000, horizon_trades=60)
    wf = await walk_forward(days=180, folds=3)

    print("=== 90 Day Summary ===")
    print(s90)
    print("=== 180 Day Summary ===")
    print(s180)
    print("=== Monte Carlo (from 180d returns) ===")
    print(mc)
    print("=== Walk Forward (3 folds, 180d) ===")
    print(wf)


if __name__ == "__main__":
    asyncio.run(main())

"""Minimal vectorbt adapter.

Takes an OHLCV DataFrame plus boolean entry/exit series and returns a portfolio
metrics dict. Synchronous on purpose — vectorbt is CPU-bound numpy.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import vectorbt as vbt  # type: ignore[import-untyped]


def run_vectorbt_backtest(
    df: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    init_cash: float = 10_000.0,
    fees: float = 0.001,
) -> dict[str, Any]:
    pf = vbt.Portfolio.from_signals(
        close=df["close"],
        entries=entries,
        exits=exits,
        init_cash=init_cash,
        fees=fees,
        freq=pd.infer_freq(df.index) or "1H",
    )
    stats = pf.stats()
    return {
        "total_return": float(stats.get("Total Return [%]", 0.0)) / 100.0,
        "sharpe": float(stats.get("Sharpe Ratio", 0.0)),
        "max_drawdown": float(stats.get("Max Drawdown [%]", 0.0)) / 100.0,
        "win_rate": float(stats.get("Win Rate [%]", 0.0)) / 100.0,
        "trades": int(stats.get("Total Trades", 0)),
    }

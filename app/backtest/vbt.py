"""Minimal vectorbt adapter.

Takes an OHLCV DataFrame plus boolean entry/exit series and returns a portfolio
metrics dict. Synchronous on purpose — vectorbt is CPU-bound numpy.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import vectorbt as vbt  # type: ignore[import-untyped]

from app.config import get_settings


# vectorbt's `freq` argument must parse as a timedelta. Pandas offset aliases
# like "W-SUN", "M", "Q-DEC" do NOT — they represent calendar anchors, not
# durations. Map the common non-timedelta aliases to a durational equivalent.
_FREQ_EQUIVALENT: dict[str, str] = {
    "D": "1D",
    "H": "1H",
}
_FREQ_PREFIX: dict[str, str] = {
    "W": "7D",
    "M": "30D",
    "MS": "30D",
    "Q": "90D",
    "QS": "90D",
    "A": "365D",
    "AS": "365D",
    "Y": "365D",
    "YS": "365D",
}


def _as_timedelta_freq(freq: str) -> str:
    """Return a duration-safe frequency string for vectorbt.

    Passes through inputs that already parse as timedeltas ("1H", "4H", "1D").
    Converts calendar-anchored offset aliases ("W-SUN", "M", "Q-DEC") to their
    approximate duration equivalents.
    """
    if not freq:
        return "1H"
    if freq in _FREQ_EQUIVALENT:
        return _FREQ_EQUIVALENT[freq]
    head = freq.split("-", 1)[0]
    if head in _FREQ_PREFIX:
        return _FREQ_PREFIX[head]
    return freq


def run_vectorbt_backtest(
    df: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    init_cash: float = 10_000.0,
    fees: float | None = None,
    sl_stop: float | None = None,
    tp_stop: float | None = None,
    freq: str | None = None,
) -> dict[str, Any]:
    if fees is None:
        # Market entries/exits — charge taker on both sides.
        fees = get_settings().binance_taker_fee
    resolved_freq = _as_timedelta_freq(freq or pd.infer_freq(df.index) or "1H")
    kwargs: dict[str, Any] = {
        "close": df["close"],
        "entries": entries,
        "exits": exits,
        "init_cash": init_cash,
        "fees": fees,
        "freq": resolved_freq,
    }
    if sl_stop is not None:
        kwargs["sl_stop"] = sl_stop
    if tp_stop is not None:
        kwargs["tp_stop"] = tp_stop
    pf = vbt.Portfolio.from_signals(**kwargs)
    stats = pf.stats()
    return {
        "total_return": float(stats.get("Total Return [%]", 0.0)) / 100.0,
        "sharpe": float(stats.get("Sharpe Ratio", 0.0)),
        "max_drawdown": float(stats.get("Max Drawdown [%]", 0.0)) / 100.0,
        "win_rate": float(stats.get("Win Rate [%]", 0.0)) / 100.0,
        "trades": int(stats.get("Total Trades", 0)),
    }

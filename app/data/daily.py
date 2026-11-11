"""Shared, fresh closed-daily input for strategy and entry gates."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.config import (
    DAILY_FETCH_BARS,
    DAILY_HISTORY_BARS,
    DAILY_MIN_USABLE_BARS,
    Timeframe,
)
from app.ta import add_indicators


def latest_daily_close(now: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    timestamp = now if now is not None else pd.Timestamp.now(tz="UTC")
    return timestamp.normalize() - pd.Timedelta(milliseconds=1)


def closed_daily_bars(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError("daily candles require a timezone-aware close-time index")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("daily candle close times must be increasing and unique")
    return df.loc[df.index <= as_of].tail(DAILY_HISTORY_BARS).copy()


async def load_daily_snapshot(
    symbol: str, *, repository=None, as_of: Optional[pd.Timestamp] = None
) -> pd.DataFrame:
    from app.data import OHLCVRepository

    repo = repository if repository is not None else OHLCVRepository()
    expected = as_of if as_of is not None else latest_daily_close()
    raw = await repo.get(symbol, Timeframe.D1, limit=DAILY_FETCH_BARS, refresh=False)
    closed = closed_daily_bars(raw, expected)
    if closed.empty or closed.index[-1] != expected or len(closed) < DAILY_HISTORY_BARS:
        raw = await repo.get(symbol, Timeframe.D1, limit=DAILY_FETCH_BARS, refresh=True)
        closed = closed_daily_bars(raw, expected)
    if closed.empty or closed.index[-1] != expected:
        raise ValueError(f"{symbol}: stale daily candles; expected close {expected}")
    if len(closed) < 200:
        raise ValueError(f"{symbol}: insufficient_history ({len(closed)} closed daily bars)")
    if not closed.index.to_series().diff().iloc[1:].eq(pd.Timedelta(days=1)).all():
        raise ValueError(f"{symbol}: daily candle history contains gaps")
    required = ["open", "high", "low", "close", "volume"]
    if closed[required].isna().any().any():
        raise ValueError(f"{symbol}: missing daily OHLCV values")
    out = add_indicators(closed).replace([float("inf"), -float("inf")], float("nan")).dropna()
    if len(out) < DAILY_MIN_USABLE_BARS:
        raise ValueError(f"{symbol}: insufficient_history ({len(out)} usable daily bars)")
    if out.index[-1] != expected:
        raise ValueError(f"{symbol}: latest daily indicators are invalid")
    out.attrs["candle_close_at"] = expected.isoformat()
    out.attrs["history_bars"] = len(closed)
    return out

"""Indicator stack used across agents.

Try pandas-ta first; fall back to `ta` (or hand-rolled) when an indicator is
unstable in the 0.3.14b beta.
"""
from __future__ import annotations

import pandas as pd

try:
    import pandas_ta as pta  # type: ignore[import-untyped]
    _HAS_PTA = True
except Exception:  # pragma: no cover - optional/buggy beta
    _HAS_PTA = False

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add a default indicator suite to an OHLCV frame.

    Expects columns: open, high, low, close, volume. Returns a new DataFrame.
    """
    out = df.copy()
    close = out["close"]
    high = out["high"]
    low = out["low"]

    if _HAS_PTA:
        try:
            out["ema_20"] = pta.ema(close, length=20)
            out["ema_50"] = pta.ema(close, length=50)
            out["ema_200"] = pta.ema(close, length=200)
            out["rsi_14"] = pta.rsi(close, length=14)
            macd = pta.macd(close)
            if macd is not None:
                out["macd"] = macd.iloc[:, 0]
                out["macd_signal"] = macd.iloc[:, 1]
                out["macd_hist"] = macd.iloc[:, 2]
            bb = pta.bbands(close, length=20)
            if bb is not None:
                out["bb_lower"] = bb.iloc[:, 0]
                out["bb_mid"] = bb.iloc[:, 1]
                out["bb_upper"] = bb.iloc[:, 2]
            out["atr_14"] = pta.atr(high, low, close, length=14)
            return out
        except Exception:  # fall through to `ta`
            pass

    out["ema_20"] = EMAIndicator(close=close, window=20).ema_indicator()
    out["ema_50"] = EMAIndicator(close=close, window=50).ema_indicator()
    out["ema_200"] = EMAIndicator(close=close, window=200).ema_indicator()
    out["rsi_14"] = RSIIndicator(close=close, window=14).rsi()
    macd = MACD(close=close)
    out["macd"] = macd.macd()
    out["macd_signal"] = macd.macd_signal()
    out["macd_hist"] = macd.macd_diff()
    bb = BollingerBands(close=close, window=20)
    out["bb_lower"] = bb.bollinger_lband()
    out["bb_mid"] = bb.bollinger_mavg()
    out["bb_upper"] = bb.bollinger_hband()
    out["atr_14"] = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
    return out

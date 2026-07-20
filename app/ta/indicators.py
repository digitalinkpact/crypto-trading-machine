"""Indicator stack used across agents.

Try pandas-ta first; fall back to `ta` (or hand-rolled) when an indicator is
unstable in the 0.3.14b beta.
"""
from __future__ import annotations

import pandas as pd
from app.logging_setup import get_logger

log = get_logger(__name__)

try:
    import pandas_ta as pta  # type: ignore[import-untyped]
    _HAS_PTA = True
except ModuleNotFoundError as e:  # pragma: no cover - optional dependency
    log.warning("pandas_ta unavailable, using ta fallback: %s", e)
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
            out["ema_9"] = pta.ema(close, length=9)
            out["ema_21"] = pta.ema(close, length=21)
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
            out["vol_sma_20"] = out["volume"].rolling(20).mean()
            return out
        except (ValueError, TypeError, KeyError) as e:  # fall through to `ta`
            log.warning("pandas_ta indicator pipeline failed, using ta fallback: %s", e)

    out["ema_20"] = EMAIndicator(close=close, window=20).ema_indicator()
    out["ema_50"] = EMAIndicator(close=close, window=50).ema_indicator()
    out["ema_200"] = EMAIndicator(close=close, window=200).ema_indicator()
    out["ema_9"] = EMAIndicator(close=close, window=9).ema_indicator()
    out["ema_21"] = EMAIndicator(close=close, window=21).ema_indicator()
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
    out["vol_sma_20"] = out["volume"].rolling(20).mean()
    return out

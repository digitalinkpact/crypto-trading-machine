"""Heuristic + sklearn regime classifier.

Currently a transparent rule-based classifier on EMA slopes and ATR. The hooks
are in place to swap in a trained `sklearn` model once labeled data exists.
"""
from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    CHOP = "chop"


class RegimeClassifier:
    """Default classifier uses EMA20/EMA50 slope and normalized ATR."""

    def __init__(self, atr_chop_threshold: float = 0.015) -> None:
        self._atr_chop_threshold = atr_chop_threshold

    def classify(self, df: pd.DataFrame) -> Regime:
        if not {"ema_20", "ema_50", "atr_14", "close"}.issubset(df.columns):
            raise ValueError("DataFrame missing indicator columns; run add_indicators first")
        last = df.dropna().iloc[-1]
        atr_pct = float(last["atr_14"]) / float(last["close"]) if last["close"] else 0.0
        if atr_pct < self._atr_chop_threshold:
            return Regime.CHOP
        slope = float(last["ema_20"] - last["ema_50"])
        if np.isclose(slope, 0):
            return Regime.CHOP
        return Regime.BULL if slope > 0 else Regime.BEAR

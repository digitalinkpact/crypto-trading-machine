"""Multi-factor BTC market-regime scorer.

Produces a -2..+2 regime score instead of a single binary EMA50/200 flag, per
the "don't use one indicator by itself" requirement. Three independent votes
are combined:
  1. Trend:  EMA50 vs EMA200 (with a small buffer to avoid noise right at the
     cross)
  2. Slope:  EMA50 direction over the trailing window (rising/falling trend)
  3. Price:  latest close vs EMA50 (is price actually participating in the
     trend, not just diverging from it)

Each vote contributes -1 / 0 / +1; the raw sum is clamped to [-2, 2].
  score >= 2  -> STRONG_BULL
  score == 1  -> BULL
  score == 0  -> SIDEWAYS
  score == -1 -> BEAR
  score <= -2 -> STRONG_BEAR
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

_TREND_BUFFER = 0.01  # 1% buffer around ema200 to avoid noise at the exact cross
_SLOPE_LOOKBACK = 5  # trading days


@dataclass
class RegimeResult:
    score: int
    label: str
    detail: dict


def _label(score: int) -> str:
    if score >= 2:
        return "STRONG_BULL"
    if score == 1:
        return "BULL"
    if score == 0:
        return "SIDEWAYS"
    if score == -1:
        return "BEAR"
    return "STRONG_BEAR"


def compute_btc_regime_score(df: pd.DataFrame) -> RegimeResult:
    """Compute the scored BTC regime from a daily OHLCV+indicator frame.

    Expects columns: close, ema_50, ema_200 (post add_indicators + dropna).
    Fails closed to SIDEWAYS (score=0) on insufficient/invalid data — callers
    that gate BUYs on regime should treat missing data as "no extra
    restriction" at the call site, not assume this function fails open.
    """
    out = df.dropna()
    if out.empty or not {"close", "ema_50", "ema_200"} <= set(out.columns):
        return RegimeResult(0, "SIDEWAYS", {"reason": "insufficient_data"})

    last = out.iloc[-1]
    ema50 = float(last["ema_50"])
    ema200 = float(last["ema_200"])
    close = float(last["close"])
    if ema200 <= 0 or ema50 <= 0:
        return RegimeResult(0, "SIDEWAYS", {"reason": "invalid_ema"})

    if len(out) > _SLOPE_LOOKBACK:
        prev_ema50 = float(out.iloc[-1 - _SLOPE_LOOKBACK]["ema_50"])
        slope_pct = (ema50 - prev_ema50) / prev_ema50 if prev_ema50 > 0 else 0.0
    else:
        # Not enough history to judge slope — that vote abstains (0) rather
        # than forcing the whole regime to "insufficient data".
        slope_pct = 0.0

    trend_vote = 0
    if ema50 >= ema200 * (1 + _TREND_BUFFER):
        trend_vote = 1
    elif ema50 <= ema200 * (1 - _TREND_BUFFER):
        trend_vote = -1

    slope_vote = 0
    if slope_pct > 0.002:
        slope_vote = 1
    elif slope_pct < -0.002:
        slope_vote = -1

    price_vote = 0
    if close >= ema50:
        price_vote = 1
    elif close < ema50:
        price_vote = -1

    raw = trend_vote + slope_vote + price_vote
    score = max(-2, min(2, raw))
    detail = {
        "ema50": ema50,
        "ema200": ema200,
        "close": close,
        "slope_pct": slope_pct,
        "trend_vote": trend_vote,
        "slope_vote": slope_vote,
        "price_vote": price_vote,
        "raw_sum": raw,
    }
    return RegimeResult(score, _label(score), detail)

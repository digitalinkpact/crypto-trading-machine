"""Tests for the multi-factor BTC regime scorer."""
from __future__ import annotations

import pandas as pd

from app.regime.btc_regime import compute_btc_regime_score


def _series(closes: list[float], ema50: list[float], ema200: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes, "ema_50": ema50, "ema_200": ema200})


def test_strong_bull_scores_plus_two():
    df = _series(
        closes=[90, 92, 94, 96, 98, 100, 102, 104],
        ema50=[70, 72, 74, 76, 78, 80, 82, 84],
        ema200=[75] * 8,
    )
    result = compute_btc_regime_score(df)
    assert result.score == 2
    assert result.label == "STRONG_BULL"


def test_strong_bear_scores_minus_two():
    df = _series(
        closes=[110, 108, 106, 104, 102, 100, 98, 96],
        ema50=[130, 128, 126, 124, 122, 120, 118, 116],
        ema200=[150] * 8,
    )
    result = compute_btc_regime_score(df)
    assert result.score == -2
    assert result.label == "STRONG_BEAR"


def test_sideways_when_votes_conflict():
    # Trend flat (ema50 within 1% buffer of ema200) but slope rising and
    # price below ema50 — conflicting votes should cancel out to SIDEWAYS,
    # not just "all zero".
    df = _series(
        closes=[100.0] * 8,
        ema50=[99.0, 99.2, 99.4, 99.6, 99.8, 100.0, 100.3, 100.6],
        ema200=[100.0] * 8,
    )
    result = compute_btc_regime_score(df)
    assert result.score == 0
    assert result.label == "SIDEWAYS"


def test_insufficient_data_fails_to_sideways():
    df = pd.DataFrame({"close": [], "ema_50": [], "ema_200": []})
    result = compute_btc_regime_score(df)
    assert result.score == 0
    assert result.detail["reason"] == "insufficient_data"


def test_short_history_abstains_slope_vote_instead_of_failing():
    """Fewer rows than the slope lookback should still score trend+price."""
    df = _series(closes=[100.0], ema50=[90.0], ema200=[100.0])
    result = compute_btc_regime_score(df)
    # trend_vote=-1 (ema50 <= ema200*0.99), slope_vote=0 (abstain), price_vote=+1 (close>=ema50)
    assert result.score == 0
    assert result.detail["slope_vote"] == 0

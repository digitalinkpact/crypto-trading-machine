"""Backtest adapter tests — no live data, no network."""
from __future__ import annotations

import pytest

from app.backtest.vbt import _as_timedelta_freq


@pytest.mark.parametrize(
    "inferred, expected",
    [
        ("W-SUN", "7D"),   # weekly: regression for the param_sweep 1w crash
        ("W-MON", "7D"),
        ("M", "30D"),
        ("MS", "30D"),
        ("Q-DEC", "90D"),
        ("A-DEC", "365D"),
        ("Y", "365D"),
        ("1H", "1H"),      # already convertible — passed through unchanged
        ("4H", "4H"),
        ("1D", "1D"),
    ],
)
def test_as_timedelta_freq(inferred, expected):
    assert _as_timedelta_freq(inferred) == expected

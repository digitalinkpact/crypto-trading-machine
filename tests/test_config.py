"""Sanity tests — never hit Binance.US from unit tests."""
from app.config import SYMBOLS, TIMEFRAMES, Timeframe, get_settings


def test_universe_size():
    assert len(SYMBOLS) == 25
    assert len(set(SYMBOLS)) == 25  # unique


def test_timeframes():
    assert TIMEFRAMES == (Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1)


def test_settings_defaults_safe():
    s = get_settings()
    assert s.dry_run is True
    assert s.paper_trading is True
    assert 0.0 < s.kelly_fraction_cap <= 1.0

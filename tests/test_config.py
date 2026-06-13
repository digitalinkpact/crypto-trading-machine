"""Sanity tests — never hit Binance.US from unit tests."""
from app.config import SYMBOLS, TIMEFRAMES, Settings, Timeframe, get_settings


def test_universe_size():
    assert len(SYMBOLS) == 25
    assert len(set(SYMBOLS)) == 25  # unique


def test_timeframes():
    assert TIMEFRAMES == (Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1)


def test_settings_defaults_safe(monkeypatch):
    # Verify the CODE defaults are safe, isolated from any local .env (which a
    # live operator may have flipped to DRY_RUN=false). Build Settings without
    # the env file and with the relevant env vars cleared.
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("PAPER_TRADING", raising=False)
    s = Settings(_env_file=None)
    assert s.dry_run is True
    assert s.paper_trading is True
    assert 0.0 < s.kelly_fraction_cap <= 1.0


def test_runtime_settings_loadable():
    # The cached runtime settings (from .env if present) must at least parse.
    s = get_settings()
    assert 0.0 < s.kelly_fraction_cap <= 1.0

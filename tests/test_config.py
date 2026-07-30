"""Sanity tests — never hit Binance.US from unit tests."""
from app.config import SYMBOLS, TIMEFRAMES, Settings, Timeframe, get_settings


def test_universe_size():
    assert len(SYMBOLS) == 9
    assert len(set(SYMBOLS)) == 9  # unique


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


def test_drawdown_breaker_default_is_more_permissive():
    s = Settings(_env_file=None)
    assert s.drawdown_circuit_breaker_pct == 0.25


def test_spread_gate_settings_can_be_relaxed(monkeypatch):
    monkeypatch.setenv("ROLLBACK_MAX_SPREAD_PCT", "0.0030")
    monkeypatch.setenv("AGGRESSIVE_MAX_SPREAD_PCT", "0.0030")
    s = Settings(_env_file=None)
    assert s.rollback_max_spread_pct == 0.0030
    assert s.aggressive_max_spread_pct == 0.0030


def test_market_regime_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MARKET_REGIME_GATE_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.market_regime_gate_enabled is False


def test_emergency_halt_can_be_disabled(monkeypatch):
    monkeypatch.setenv("EMERGENCY_HALT_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.emergency_halt_enabled is False


def test_execution_defaults_are_more_permissive():
    s = Settings(_env_file=None)
    assert s.ml_gate_enabled is False
    assert s.max_open_positions == 10
    assert s.rollback_max_open_positions == 10
    assert s.aggressive_max_open_positions == 10


def test_live_mode_forces_live_flags():
    s = Settings(_env_file=None, live_mode=True, paper_trading=True, dry_run=True)
    assert s.live_mode is True
    assert s.paper_trading is False
    assert s.dry_run is False


def test_live_mode_from_env_forces_live(monkeypatch):
    monkeypatch.setenv("LIVE_MODE", "true")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    s = Settings(_env_file=None)
    assert s.live_mode is True
    assert s.paper_trading is False
    assert s.dry_run is False


def test_live_mode_disables_ml_gate(monkeypatch):
    monkeypatch.setenv("LIVE_MODE", "true")
    monkeypatch.setenv("ML_GATE_ENABLED", "true")
    s = Settings(_env_file=None)
    assert s.live_mode is True
    assert s.ml_gate_enabled is False


def test_live_mode_relaxes_risk_caps(monkeypatch):
    monkeypatch.setenv("LIVE_MODE", "true")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    monkeypatch.setenv("MAX_LONG_EXPOSURE_PCT", "0.60")
    s = Settings(_env_file=None)
    assert s.live_mode is True
    assert s.max_open_positions == 25
    assert s.aggressive_max_open_positions == 25
    assert s.rollback_max_open_positions == 25
    assert s.max_long_exposure_pct == 1.0

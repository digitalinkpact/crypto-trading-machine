"""Tests for the online regime → dynamic threshold delta (bounded, no network)."""
from __future__ import annotations

from app.config import get_settings
from app.regime.online import OnlineRegime


def _rows(win_rate: float, atr_pct: float, n: int = 60):
    wins = int(round(win_rate * n))
    rows = []
    for i in range(n):
        rows.append({
            "atr_pct": atr_pct,
            "ema_gap_pct": 0.01,
            "rsi_14": 55.0,
            "confidence": 0.7,
            "outcome_win": 1 if i < wins else 0,
        })
    return rows


def test_disabled_returns_zero_delta(monkeypatch):
    s = get_settings().model_copy(update={"dynamic_threshold_enabled": False})
    reg = OnlineRegime(settings=s)
    assert reg.threshold_delta() == (0.0, "disabled")


def test_insufficient_data_is_neutral(monkeypatch):
    s = get_settings().model_copy(update={
        "dynamic_threshold_enabled": True,
        "online_regime_min_samples": 30,
    })
    reg = OnlineRegime(settings=s)
    monkeypatch.setattr("app.regime.online.storage.training_signal_rows",
                        lambda limit=100: _rows(0.6, 0.02, n=5))
    delta, info = reg.threshold_delta()
    assert delta == 0.0
    assert "insufficient" in info


def test_delta_within_bounds_and_risk_on_when_winning(monkeypatch):
    s = get_settings().model_copy(update={
        "dynamic_threshold_enabled": True,
        "online_regime_min_samples": 30,
        "dynamic_threshold_max_delta": 0.10,
    })
    reg = OnlineRegime(settings=s)
    monkeypatch.setattr("app.regime.online.storage.training_signal_rows",
                        lambda limit=100: _rows(0.9, 0.01, n=60))
    delta, _ = reg.threshold_delta()
    assert -0.10 <= delta <= 0.10
    # High win-rate, low vol → favorable → lower the bar (negative delta).
    assert delta < 0


def test_delta_risk_off_when_losing(monkeypatch):
    s = get_settings().model_copy(update={
        "dynamic_threshold_enabled": True,
        "online_regime_min_samples": 30,
        "dynamic_threshold_max_delta": 0.10,
    })
    reg = OnlineRegime(settings=s)
    monkeypatch.setattr("app.regime.online.storage.training_signal_rows",
                        lambda limit=100: _rows(0.1, 0.06, n=60))
    delta, _ = reg.threshold_delta()
    assert -0.10 <= delta <= 0.10
    # Low win-rate, high vol → unfavorable → raise the bar (positive delta).
    assert delta > 0

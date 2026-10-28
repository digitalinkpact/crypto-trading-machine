"""Post-trade risk bounds, including additions to an existing position."""
from decimal import Decimal

import pytest

from app.config import get_settings
from app.trading import risk_manager


@pytest.fixture
def manager(monkeypatch):
    settings = get_settings().model_copy(update={
        "aggressive_max_open_positions": 12,
        "aggressive_position_pct": 0.06,
        "rollback_position_pct": 0.03,
        "max_long_exposure_pct": 0.60,
        "kelly_fraction_cap": 0.25,
        "risk_per_trade_pct": 0.01,
        "stop_loss_pct": 0.04,
        "atr_stop_enabled": True,
        "atr_stop_max_pct": 0.08,
        "pyramid_add_fraction": 0.50,
    })
    monkeypatch.setattr(risk_manager, "get_settings", lambda: settings)
    monkeypatch.setattr(risk_manager.storage, "closed_trades", lambda limit=100: [])
    return risk_manager.RiskManager()


def _entry(manager, **overrides):
    return manager.evaluate_entry(**{
        "mode": "live",
        "total_equity_usdt": Decimal("1000"),
        "open_positions": 1,
        "long_exposure_pct": 0.20,
        "entry_price": Decimal("100"),
        "aggressive_mode": True,
        **overrides,
    })


def test_new_position_is_capped_at_remaining_exposure(manager):
    decision = _entry(manager, long_exposure_pct=0.59)
    assert decision.allow
    assert decision.notional_usdt == Decimal("10")
    assert Decimal("590") + decision.notional_usdt <= Decimal("600")


def test_pyramid_is_capped_at_remaining_position_budget(manager):
    decision = _entry(
        manager, is_pyramid=True, current_position_notional=Decimal("55")
    )
    assert decision.allow
    assert decision.notional_usdt == Decimal("5")


@pytest.mark.parametrize("notional", ["60", "70"])
def test_pyramid_cannot_add_at_or_above_position_cap(manager, notional):
    decision = _entry(
        manager, is_pyramid=True, current_position_notional=Decimal(notional)
    )
    assert not decision.allow
    assert decision.notional_usdt == 0


def test_normal_entry_preserves_configured_position_size(manager):
    decision = _entry(manager)
    assert decision.allow
    assert decision.notional_usdt == Decimal("60")


def test_risk_budget_covers_widest_allowed_atr_stop(manager, monkeypatch):
    settings = risk_manager.get_settings().model_copy(update={
        "risk_per_trade_pct": 0.001,
    })
    monkeypatch.setattr(risk_manager, "get_settings", lambda: settings)
    decision = _entry(manager)
    assert decision.allow
    assert decision.notional_usdt == Decimal("12.5")
    assert decision.notional_usdt * Decimal("0.08") <= Decimal("1")


def test_pyramid_cannot_exceed_remaining_stop_risk_budget(manager, monkeypatch):
    settings = risk_manager.get_settings().model_copy(update={
        "risk_per_trade_pct": 0.001,
    })
    monkeypatch.setattr(risk_manager, "get_settings", lambda: settings)
    decision = _entry(
        manager, is_pyramid=True, current_position_notional=Decimal("12")
    )
    assert decision.allow
    assert decision.notional_usdt == Decimal("0.5")


def test_disabled_atr_uses_fixed_stop_for_sizing(manager, monkeypatch):
    settings = risk_manager.get_settings().model_copy(update={
        "risk_per_trade_pct": 0.001,
        "atr_stop_enabled": False,
    })
    monkeypatch.setattr(risk_manager, "get_settings", lambda: settings)
    decision = _entry(manager)
    assert decision.notional_usdt == Decimal("25")

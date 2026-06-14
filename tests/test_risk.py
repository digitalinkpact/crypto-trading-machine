"""Tests for risk gates — stop-loss, take-profit, max-hold, drawdown breaker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import get_settings
from app.trading import risk


def _pos(symbol: str, qty: float, entry: float, hours_ago: int = 1) -> dict:
    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry,
        "entry_ts": (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
        "mode": "paper",
        "agents": "[]",
    }


def test_stop_loss_triggers():
    """Position down >2% should hit stop_loss exit."""
    positions = [_pos("BTCUSDT", 1.0, 100.0)]
    prices = {"BTCUSDT": Decimal("97")}  # -3%
    risk.clear_hwm("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert len(exits) == 1
    assert exits[0].reason == "stop_loss"


def test_take_profit_triggers():
    """Position up >5% should hit take_profit exit."""
    positions = [_pos("BTCUSDT", 1.0, 100.0)]
    prices = {"BTCUSDT": Decimal("106")}  # +6%
    risk.clear_hwm("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert len(exits) == 1
    assert exits[0].reason == "take_profit"


def test_no_exit_when_within_band():
    """Position +1% should not exit."""
    positions = [_pos("BTCUSDT", 1.0, 100.0)]
    prices = {"BTCUSDT": Decimal("101")}
    risk.clear_hwm("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert exits == []


def test_max_hold_triggers():
    """Position held longer than max_hold_hours should force-exit."""
    positions = [_pos("BTCUSDT", 1.0, 100.0, hours_ago=200)]  # default max_hold=96h
    prices = {"BTCUSDT": Decimal("100.5")}  # within band
    risk.clear_hwm("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert len(exits) == 1
    assert exits[0].reason == "max_hold"


def test_circuit_breaker():
    tripped, dd = risk.is_circuit_breaker_tripped(
        starting_balance=Decimal("10000"),
        current_balance=Decimal("8900"),  # -11%
    )
    assert tripped is True
    assert dd < -0.10

    tripped, _ = risk.is_circuit_breaker_tripped(
        starting_balance=Decimal("10000"),
        current_balance=Decimal("9500"),  # -5%
    )
    assert tripped is False


def test_volatility_scaled_pct():
    # Quiet coin (1% ATR) → bigger size
    bigger = risk.volatility_scaled_pct(0.05, atr_pct=0.01)
    assert bigger > 0.05

    # Wild coin (8% ATR) → smaller size
    smaller = risk.volatility_scaled_pct(0.05, atr_pct=0.08)
    assert smaller < 0.05

    # Clamps respected
    extreme_quiet = risk.volatility_scaled_pct(0.05, atr_pct=0.0001)
    assert extreme_quiet <= 0.05 * 1.5 + 1e-9


def test_max_open_positions_cap():
    s = get_settings()
    ok, _ = risk.can_open_new_position(
        open_positions=max(0, s.max_open_positions - 1),
        long_exposure_pct=0.20,
    )
    assert ok is True
    blocked, why = risk.can_open_new_position(
        open_positions=s.max_open_positions,
        long_exposure_pct=0.20,
    )
    assert blocked is False
    assert "max_open_positions" in why


def test_max_long_exposure_cap():
    blocked, why = risk.can_open_new_position(open_positions=1, long_exposure_pct=0.65)
    assert blocked is False
    assert "long_exposure" in why

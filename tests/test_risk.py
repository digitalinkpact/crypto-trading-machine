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
    """Position down past the stop band should hit stop_loss exit."""
    positions = [_pos("BTCUSDT", 1.0, 100.0)]
    prices = {"BTCUSDT": Decimal("95")}  # -5% (beyond the 4% hard stop)
    risk.clear_hwm("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert len(exits) == 1
    assert exits[0].reason == "stop_loss"


def test_take_profit_triggers():
    """Position up past the TP1 scale-out band should hit take_profit_1 exit."""
    positions = [_pos("BTCUSDT", 1.0, 100.0)]
    prices = {"BTCUSDT": Decimal("109")}  # +9% (past the 8% TP1 trigger)
    risk.clear_hwm("BTCUSDT")
    risk.clear_tp1("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert len(exits) == 1
    assert exits[0].reason == "take_profit_1"


def test_take_profit_2_triggers_after_tp1():
    """After TP1 has scaled out 50%, a further move past the TP2 band (+15%)
    should sell another 25% of the ORIGINAL stake, leaving the rest to the
    trailing stop (no more fixed final-target full close)."""
    # Simulate the post-TP1 state: 1.0 original qty -> 0.5 remaining.
    positions = [_pos("BTCUSDT", 0.5, 100.0)]
    prices = {"BTCUSDT": Decimal("116")}  # +16% (past the 15% TP2 trigger)
    risk.clear_hwm("BTCUSDT")
    risk.clear_tp1("BTCUSDT")
    risk.clear_tp2("BTCUSDT")
    risk.mark_tp1_taken("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert len(exits) == 1
    assert exits[0].reason == "take_profit_2"
    # 25% of the original 1.0 stake, back-derived from the 0.5 remaining
    # after a 50% TP1 scale-out.
    assert exits[0].qty == Decimal("0.25")
    risk.clear_tp1("BTCUSDT")
    risk.clear_tp2("BTCUSDT")


def test_remainder_after_tp1_and_tp2_rides_trailing_stop():
    """Once both scale-outs have fired, the remaining ~25% should NOT be
    force-closed by any fixed profit target — only stop-loss/trailing/max-hold
    apply to it from here."""
    positions = [_pos("BTCUSDT", 0.25, 100.0)]
    prices = {"BTCUSDT": Decimal("130")}  # well past both TP1/TP2 bands
    risk.clear_hwm("BTCUSDT")
    risk.clear_tp1("BTCUSDT")
    risk.clear_tp2("BTCUSDT")
    risk.mark_tp1_taken("BTCUSDT")
    risk.mark_tp2_taken("BTCUSDT")
    exits = risk.evaluate_exits(positions=positions, prices=prices)
    assert exits == []
    risk.clear_tp1("BTCUSDT")
    risk.clear_tp2("BTCUSDT")


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
        current_balance=Decimal("7400"),  # -26%
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


def test_daily_loss_limit_trips_on_todays_realized_losses(monkeypatch):
    """Distinct from the cumulative drawdown breaker: sums only TODAY's
    realized pnl and halts new BUYs once it exceeds daily_loss_limit_pct."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1, hours=1)
    trades = [
        {"mode": "live", "pnl": -60.0, "exit_ts": now.isoformat()},
        {"mode": "live", "pnl": -50.0, "exit_ts": now.isoformat()},
        # Old loss from a prior day must NOT count toward today's total.
        {"mode": "live", "pnl": -500.0, "exit_ts": yesterday.isoformat()},
        # Different mode must not bleed into this mode's total.
        {"mode": "paper", "pnl": -1000.0, "exit_ts": now.isoformat()},
    ]
    monkeypatch.setattr(risk.storage, "closed_trades", lambda limit=500: trades, raising=True)

    tripped, today_pnl = risk.is_daily_loss_limit_tripped(
        mode="live", starting_balance=Decimal("1000"), now=now
    )
    assert tripped is True  # -110 realized vs 5% of 1000 = -50 limit
    assert today_pnl == Decimal("-110.0")


def test_daily_loss_limit_not_tripped_within_band(monkeypatch):
    now = datetime.now(timezone.utc)
    trades = [{"mode": "live", "pnl": -10.0, "exit_ts": now.isoformat()}]
    monkeypatch.setattr(risk.storage, "closed_trades", lambda limit=500: trades, raising=True)

    tripped, today_pnl = risk.is_daily_loss_limit_tripped(
        mode="live", starting_balance=Decimal("1000"), now=now
    )
    assert tripped is False
    assert today_pnl == Decimal("-10.0")


def test_daily_loss_limit_disabled_never_trips(monkeypatch):
    class _S:
        daily_loss_limit_enabled = False
        daily_loss_limit_pct = 0.05

    monkeypatch.setattr(risk, "get_settings", lambda: _S())
    tripped, pnl = risk.is_daily_loss_limit_tripped(
        mode="live", starting_balance=Decimal("1000")
    )
    assert tripped is False
    assert pnl == Decimal("0")

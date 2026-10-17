"""Unit tests for the strategy_lab backtest additions: ATR-stop modelling, the
stale_dead_money exit, and per-position aggregation. All deterministic — no
network, no DB (synthetic frames only)."""
from __future__ import annotations

import pandas as pd

from scripts.strategy_lab import (
    LadderParams,
    Trade,
    _entry_stop_frac,
    aggregate_positions,
    default_params,
    positions_from_trades,
    simulate_symbol,
)


def _flat_frame() -> pd.DataFrame:
    # +0.1%/bar drift — never hits a 3% stop, 8% TP1, or +5% trailing arm.
    close = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5]
    return pd.DataFrame(
        {
            "close": close,
            "low": close,
            "rsi_14": [50.0] * len(close),      # < 55 so the RSI exit never fires
            "atr_14": [0.5] * len(close),       # atr_pct 0.5% -> ATR stop clamps to 3% min
            "macd_hist": [0.0] * len(close),
            "ema_20": close,
        }
    )


def test_entry_stop_frac_clamps_to_band():
    p = default_params(atr_stop_min=0.03, atr_stop_max=0.08, atr_stop_multiple=2.0)
    hi = pd.DataFrame({"close": [100.0], "atr_14": [10.0]})   # atr_pct 10% -> raw 20% -> cap 8%
    lo = pd.DataFrame({"close": [100.0], "atr_14": [0.5]})    # atr_pct 0.5% -> raw 1% -> floor 3%
    assert _entry_stop_frac(hi, 0, p) == 0.08
    assert _entry_stop_frac(lo, 0, p) == 0.03
    off = default_params(atr_stop_enabled=False, stop_loss_pct=0.04)
    assert _entry_stop_frac(hi, 0, off) == 0.04  # falls back to fixed stop


def test_stale_dead_money_exit_fires_on_flat_position():
    df = _flat_frame()
    params = default_params(
        stale_exit_enabled=True, stale_exit_days=2, stale_exit_max_pnl=0.02,
        max_hold_days=100, tp1_pct=0.9, trail_activation=0.9,
    )
    trades = simulate_symbol("BTCUSDT", df, lambda d, i: i == 0, params)
    assert len(trades) == 1
    assert trades[0].exit_reason == "stale_dead_money"
    assert trades[0].exit_idx == 2  # 2 days after the bar-0 entry


def test_stale_disabled_rides_to_max_hold():
    df = _flat_frame()
    params = default_params(
        stale_exit_enabled=False, max_hold_days=4, tp1_pct=0.9, trail_activation=0.9,
    )
    trades = simulate_symbol("BTCUSDT", df, lambda d, i: i == 0, params)
    assert len(trades) == 1
    assert trades[0].exit_reason == "max_hold"
    assert trades[0].exit_idx == 4


def test_positions_from_trades_collapses_partial_legs():
    legs = [
        # One winning position scaled out across TP1 + trailing (same entry).
        Trade("BTCUSDT", 0, 2, 100.0, 108.0, 0.5, 0.08, "take_profit_1", 0.10, 0.02),
        Trade("BTCUSDT", 0, 5, 100.0, 112.0, 0.5, 0.12, "trailing_stop", 0.15, 0.02),
        # A separate losing single-leg position.
        Trade("ETHUSDT", 0, 3, 100.0, 95.0, 1.0, -0.05, "stop_loss", 0.0, 0.05),
    ]
    positions = positions_from_trades(legs)
    assert len(positions) == 2
    by_sym = {p.symbol: p for p in positions}
    # Position pnl% on full notional = 0.08*0.5 + 0.12*0.5 = 0.10, reason = last leg.
    assert round(by_sym["BTCUSDT"].pnl_pct, 6) == 0.10
    assert by_sym["BTCUSDT"].exit_reason == "trailing_stop"
    assert by_sym["ETHUSDT"].pnl_pct == -0.05
    # Per-position win rate: 1 win / 2 positions, not 2 wins / 3 legs.
    assert aggregate_positions(legs).win_rate == 0.5


def test_entry_pullback_mirrors_live_rule():
    from scripts.strategy_lab import entry_pullback

    def frame(close, ema20, ema50, rsi):
        return pd.DataFrame({"close": [close], "ema_20": [ema20], "ema_50": [ema50], "rsi_14": [rsi]})

    assert entry_pullback(frame(103.0, 100.0, 95.0, 50.0), 0)          # above both EMAs, 3% off EMA20
    assert not entry_pullback(frame(108.0, 100.0, 95.0, 50.0), 0)      # >7% extended from EMA20
    assert not entry_pullback(frame(99.0, 100.0, 95.0, 50.0), 0)       # below EMA20
    assert not entry_pullback(frame(103.0, 100.0, 104.0, 50.0), 0)     # below EMA50
    assert not entry_pullback(frame(103.0, 100.0, 95.0, 75.0), 0)      # RSI overbought
    assert not entry_pullback(frame(103.0, 100.0, 95.0, 30.0), 0)      # RSI too low


def test_mean_reversion_exit_respects_enabled_flag():
    # RSI jumps above the exit threshold with a small gain: fires only when enabled.
    close = [100.0, 101.0, 101.0]
    df = pd.DataFrame({"close": close, "low": close, "rsi_14": [50.0, 70.0, 70.0],
                       "atr_14": [0.5] * 3, "macd_hist": [0.0] * 3, "ema_20": close})
    common = dict(mean_reversion_rsi=55, mean_reversion_min_pnl=0.0,
                  mean_reversion_require_confirmation=False, stale_exit_enabled=False,
                  max_hold_days=10, tp1_pct=0.9, trail_activation=0.9)
    on = simulate_symbol("X", df, lambda d, i: i == 0, default_params(mean_reversion_enabled=True, **common))
    off = simulate_symbol("X", df, lambda d, i: i == 0, default_params(mean_reversion_enabled=False, **common))
    assert on[0].exit_reason == "mean_reversion_rsi"
    assert not off  # no exit fired; position still open at end of frame


def test_breakeven_after_tp1_replaces_hard_stop():
    # +10% bar fills TP1 (50%), then price round-trips to -1%: with breakeven
    # the remainder exits at entry (breakeven_stop) instead of waiting for -8%.
    close = [100.0, 110.0, 99.0, 99.0]
    df = pd.DataFrame({"close": close, "low": close, "rsi_14": [50.0] * 4, "atr_14": [10.0] * 4,
                       "macd_hist": [0.0] * 4, "ema_20": close})
    common = dict(tp1_pct=0.08, tp1_frac=0.5, tp2_pct=0.5, trail_activation=0.5,
                  stale_exit_enabled=False, max_hold_days=10, atr_stop_max=0.08)
    be = simulate_symbol("X", df, lambda d, i: i == 0, default_params(breakeven_after_tp1=True, **common))
    assert [t.exit_reason for t in be] == ["take_profit_1", "breakeven_stop"]
    assert be[1].qty == 0.5
    no = simulate_symbol("X", df, lambda d, i: i == 0, default_params(breakeven_after_tp1=False, **common))
    assert [t.exit_reason for t in no] == ["take_profit_1"]  # -1% never reaches the -8% stop

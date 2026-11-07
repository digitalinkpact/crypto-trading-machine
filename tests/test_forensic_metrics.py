"""Tests for the closed-trade forensic metrics added alongside the RSI-exit
rework: MFE/MAE, holding duration, and entry-context (confidence/strategy/
BTC regime) carried from `positions` through to `closed_trades`, plus the
explicit (non-generic) exit_reason threading via `risk.infer_exit_reason`.
"""
from __future__ import annotations

import pytest

from app.storage.db import Storage
from app.trading.risk import infer_exit_reason


def _fresh_storage(tmp_path) -> Storage:
    return Storage(path=tmp_path / "t.db")


def test_open_position_persists_entry_context(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="paper", qty=1.0, entry_price=100.0,
        agents=["profitstream_strategy"],
        entry_confidence=0.9, entry_strategy="dip_buy", entry_btc_regime=2,
    )
    pos = s.get_position("ETHUSDT")
    assert pos["entry_confidence"] == 0.9
    assert pos["entry_strategy"] == "dip_buy"
    assert pos["entry_btc_regime"] == 2


def test_pyramid_add_does_not_overwrite_original_entry_context(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0,
        agents=["profitstream_strategy"],
        entry_confidence=0.9, entry_strategy="dip_buy", entry_btc_regime=2,
    )
    # A pyramid add with different (or missing) forensic context must not
    # clobber the original entry's metadata.
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=1.0, entry_price=110.0,
        agents=["profitstream_strategy"],
        entry_confidence=None, entry_strategy=None, entry_btc_regime=None,
    )
    pos = s.get_position("ETHUSDT")
    assert pos["qty"] == 2.0
    assert pos["entry_strategy"] == "dip_buy"
    assert pos["entry_confidence"] == 0.9
    assert pos["entry_btc_regime"] == 2


def test_close_position_persists_mfe_mae_and_holding_hours(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="paper", qty=1.0, entry_price=100.0,
        agents=["profitstream_strategy"],
        entry_confidence=0.9, entry_strategy="dip_buy", entry_btc_regime=1,
    )
    closed = s.close_position(
        symbol="ETHUSDT", mode="paper", exit_price=105.0,
        exit_reason="mean_reversion_rsi_price", mfe_pct=0.12, mae_pct=0.03,
    )
    assert closed["mfe_pct"] == 0.12
    assert closed["mae_pct"] == 0.03
    assert closed["holding_hours"] is not None and closed["holding_hours"] >= 0

    trades = s.closed_trades(limit=5)
    row = trades[0]
    assert row["exit_reason"] == "mean_reversion_rsi_price"
    assert row["mfe_pct"] == 0.12
    assert row["mae_pct"] == 0.03
    assert row["entry_confidence"] == 0.9
    assert row["entry_strategy"] == "dip_buy"
    assert row["entry_btc_regime"] == 1


def test_reduce_position_persists_mfe_mae_on_partial_close(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=2.0, entry_price=100.0,
        agents=["profitstream_strategy"],
        entry_confidence=0.8, entry_strategy="oversold_bounce", entry_btc_regime=2,
    )
    result = s.reduce_position(
        symbol="ETHUSDT", mode="live", qty=1.0, exit_price=108.0,
        exit_reason="take_profit_1", mfe_pct=0.09, mae_pct=0.0,
    )
    assert result["remaining_qty"] == 1.0
    assert result["mfe_pct"] == 0.09

    trades = s.closed_trades(limit=5)
    row = trades[0]
    assert row["exit_reason"] == "take_profit_1"
    assert row["entry_strategy"] == "oversold_bounce"
    assert row["entry_btc_regime"] == 2
    # Position still open with the remainder.
    pos = s.get_position("ETHUSDT")
    assert pos["qty"] == 1.0


def test_infer_exit_reason_prioritizes_risk_over_signal_exit_tag():
    assert infer_exit_reason(["risk:stop_loss", "exit:mean_reversion_rsi"]) == "stop_loss"


def test_infer_exit_reason_uses_explicit_signal_exit_tag():
    assert infer_exit_reason(["profitstream_strategy", "exit:mean_reversion_rsi_momentum"]) == "mean_reversion_rsi_momentum"


def test_infer_exit_reason_falls_back_to_generic_signal():
    assert infer_exit_reason(["profitstream_strategy"]) == "signal"


def test_close_position_uses_modeled_fee_when_no_actual_fee_given(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="paper", qty=1.0, entry_price=100.0, agents=[])
    closed = s.close_position(symbol="ETHUSDT", mode="paper", exit_price=105.0)
    assert closed["fee_source"] == "modeled"
    assert closed["gross_pnl"] == 5.0
    assert closed["fee_amount"] > 0
    assert closed["pnl"] == closed["gross_pnl"] - closed["fee_amount"]

    row = s.closed_trades(limit=1)[0]
    assert row["fee_source"] == "modeled"
    # pnl_corrected/gross_pnl are populated for every new row (not just backfill).
    assert row["gross_pnl"] == 5.0
    assert row["pnl_corrected"] == row["pnl"]


def test_close_position_uses_actual_fee_when_provided(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[])
    closed = s.close_position(
        symbol="ETHUSDT", mode="live", exit_price=105.0, actual_fee_usdt=0.03,
    )
    # Exit fee is the real commission; the unknown buy-side fee is modeled
    # (100 * 1 * 0.0002 taker) instead of silently dropped.
    assert closed["fee_source"] == "mixed"
    assert closed["fee_amount"] == pytest.approx(0.03 + 0.02)
    assert closed["pnl"] == pytest.approx(5.0 - 0.05)


def test_close_position_actual_fee_with_known_entry_fee_is_fully_actual(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[],
        entry_fee_usdt=0.02,
    )
    closed = s.close_position(
        symbol="ETHUSDT", mode="live", exit_price=105.0, actual_fee_usdt=0.03,
    )
    assert closed["fee_source"] == "actual"
    assert closed["fee_amount"] == pytest.approx(0.05)


def test_reduce_position_uses_actual_fee_when_provided(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=2.0, entry_price=100.0, agents=[])
    result = s.reduce_position(
        symbol="ETHUSDT", mode="live", qty=1.0, exit_price=108.0, actual_fee_usdt=0.02,
    )
    assert result["fee_source"] == "actual"
    assert result["fee_amount"] == 0.02
    assert result["pnl"] == 8.0 - 0.02


def test_partial_exits_allocate_entry_fee_once_across_ladder(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[],
        entry_fee_usdt=0.10,
    )
    first = s.reduce_position(
        symbol="ETHUSDT", mode="live", qty=0.5, exit_price=110.0,
        actual_exit_fee_usdt=0.02,
    )
    second = s.reduce_position(
        symbol="ETHUSDT", mode="live", qty=0.5, exit_price=120.0,
        actual_exit_fee_usdt=0.03,
    )
    assert first["fee_amount"] == pytest.approx(0.07)
    assert second["fee_amount"] == pytest.approx(0.08)
    assert first["fee_amount"] + second["fee_amount"] == pytest.approx(0.15)


def test_open_position_accumulates_entry_fee_across_pyramid_adds(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[],
        entry_fee_usdt=0.02,
    )
    s.open_position(
        symbol="ETHUSDT", mode="live", qty=1.0, entry_price=110.0, agents=[],
        entry_fee_usdt=0.022,
    )
    pos = s.get_position("ETHUSDT")
    assert pos["entry_fee_usdt"] == pytest.approx(0.042)


# ── win-rate / fee integrity (2026-11 forensic restoration) ────────────────


def test_reduce_position_charges_modeled_entry_fee_when_entry_fee_unknown(tmp_path):
    """Live BUY commission is usually paid in the base coin, so entry_fee_usdt is
    None. The exit-only fee used to be booked alone, overstating PnL."""
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[])
    r = s.reduce_position(
        symbol="ETHUSDT", mode="live", qty=1.0, exit_price=100.03,
        actual_exit_fee_usdt=0.02,
    )
    assert r["fee_source"] == "mixed"
    assert r["fee_amount"] == pytest.approx(0.02 + 0.02)  # exit actual + modeled entry
    assert r["pnl"] < 0  # a +0.03% gross "win" is a fee-adjusted loss


def test_partial_reduce_keeps_unknown_entry_fee_unknown(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=2.0, entry_price=100.0, agents=[])
    s.reduce_position(symbol="ETHUSDT", mode="live", qty=1.0, exit_price=108.0,
                      actual_exit_fee_usdt=0.02)
    assert s.get_position("ETHUSDT")["entry_fee_usdt"] is None


def test_bookkeeping_closes_carry_no_fee_and_no_win_rate_weight(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="AAAUSDT", mode="live", qty=1.0, entry_price=50.0, agents=["a"])
    closed = s.close_position(
        symbol="AAAUSDT", mode="live", exit_price=50.0, exit_reason="stop_loss_stale_dust",
    )
    assert closed["fee_amount"] == 0.0 and closed["pnl"] == 0.0
    s.open_position(symbol="BBBUSDT", mode="live", qty=1.0, entry_price=50.0, agents=["a"])
    s.close_position(symbol="BBBUSDT", mode="live", exit_price=50.0, exit_reason="reconcile_stale")

    assert s.closed_positions(limit=10, mode="live") == []
    assert s.agent_stats() == []  # phantom rows never credit/debit an agent


def test_closed_positions_excludes_still_open_partials(tmp_path):
    """A TP1 leg is positive on its own; the position is not closed (and not a
    win) until the remainder exits."""
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=2.0, entry_price=100.0, agents=[])
    s.reduce_position(symbol="ETHUSDT", mode="live", qty=1.0, exit_price=108.0,
                      exit_reason="take_profit_1", actual_exit_fee_usdt=0.02)
    assert s.closed_positions(limit=10, mode="live") == []

    s.reduce_position(symbol="ETHUSDT", mode="live", qty=1.0, exit_price=90.0,
                      exit_reason="stop_loss", actual_exit_fee_usdt=0.02)
    positions = s.closed_positions(limit=10, mode="live")
    assert len(positions) == 1
    assert positions[0]["rows"] == 2
    assert positions[0]["pnl"] < 0  # +8 leg then -10 leg: a LOSING position


def test_agent_stats_credit_one_outcome_per_position_not_per_leg(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=3.0, entry_price=100.0, agents=["x"])
    for qty, px, reason in ((1.0, 108.0, "take_profit_1"), (1.0, 115.0, "take_profit_2"),
                            (1.0, 112.0, "trailing_stop")):
        s.reduce_position(symbol="ETHUSDT", mode="live", qty=qty, exit_price=px,
                          exit_reason=reason, actual_exit_fee_usdt=0.02)
    stats = {r["agent"]: r for r in s.agent_stats()}
    assert stats["x"]["wins"] == 1 and stats["x"]["losses"] == 0

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
    assert closed["fee_source"] == "actual"
    assert closed["fee_amount"] == 0.03
    assert closed["pnl"] == 5.0 - 0.03


def test_reduce_position_uses_actual_fee_when_provided(tmp_path):
    s = _fresh_storage(tmp_path)
    s.open_position(symbol="ETHUSDT", mode="live", qty=2.0, entry_price=100.0, agents=[])
    result = s.reduce_position(
        symbol="ETHUSDT", mode="live", qty=1.0, exit_price=108.0, actual_fee_usdt=0.02,
    )
    assert result["fee_source"] == "actual"
    assert result["fee_amount"] == 0.02
    assert result["pnl"] == 8.0 - 0.02


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

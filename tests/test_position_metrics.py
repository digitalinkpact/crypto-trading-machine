"""scripts/position_metrics groups closed_trades LEGS into POSITIONS."""
from __future__ import annotations

from app.storage.db import Storage
from scripts.position_metrics import load_positions, summarize


def test_ladder_legs_count_as_one_position(tmp_path):
    s = Storage(path=tmp_path / "t.db")
    s.open_position(symbol="ETHUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[])
    s.reduce_position(symbol="ETHUSDT", mode="live", qty=0.5, exit_price=106.0, exit_reason="take_profit_1")
    s.reduce_position(symbol="ETHUSDT", mode="live", qty=0.5, exit_price=97.0, exit_reason="stop_loss")
    s.open_position(symbol="BTCUSDT", mode="live", qty=1.0, entry_price=100.0, agents=[])
    s.close_position(symbol="BTCUSDT", mode="live", exit_price=96.0, exit_reason="stop_loss")

    positions = load_positions("live", db_path=tmp_path / "t.db")
    assert len(positions) == 2
    eth = next(p for p in positions if p.symbol == "ETHUSDT")
    assert len(eth.legs) == 2
    assert eth.exit_path == "take_profit_1>stop_loss"
    assert eth.final_exit == "stop_loss"
    assert eth.pnl > 0  # +3 -1.5 minus fees: the ladder nets a win

    summary = summarize(positions)
    assert summary["positions"] == 2 and summary["legs"] == 3
    assert summary["win_rate"] == 0.5  # row-level would say 1/3
    assert summary["max_losing_streak"] == 1


def test_exclude_ids_and_dust_legs(tmp_path):
    s = Storage(path=tmp_path / "t.db")
    s.open_position(symbol="ADAUSDT", mode="live", qty=100.0, entry_price=1.0, agents=[])
    s.reduce_position(symbol="ADAUSDT", mode="live", qty=99.0, exit_price=0.95, exit_reason="stop_loss")
    s.reduce_position(symbol="ADAUSDT", mode="live", qty=1.0, exit_price=1.0, exit_reason="stop_loss_stale_dust")
    positions = load_positions("live", db_path=tmp_path / "t.db")
    assert positions[0].exit_path == "stop_loss"
    assert load_positions("live", db_path=tmp_path / "t.db", exclude_ids=[1, 2]) == []

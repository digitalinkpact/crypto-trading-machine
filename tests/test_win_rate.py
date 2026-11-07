"""One definition of win rate: position-level (target) vs row-level (legacy)."""
from __future__ import annotations

import pytest

from app.storage.db import Storage
from app.trading.win_rate import (
    REBASELINE_NOTE,
    collapse_to_positions,
    format_win_rate_block,
    open_position_keys,
    win_rate_summary,
)


def _row(i, symbol, entry_ts, pnl, reason="stop_loss", gross=None, mode="live"):
    return {"id": i, "symbol": symbol, "mode": mode, "entry_ts": entry_ts, "pnl": pnl,
            "gross_pnl": gross, "exit_reason": reason, "entry_price": 100.0, "qty": 1.0,
            "pnl_pct": pnl}


def test_summary_reports_both_definitions_and_what_was_excluded():
    rows = [
        # winner scaled out in 3 legs
        _row(1, "A", "t1", 5.0, "take_profit_1"), _row(2, "A", "t1", 3.0, "take_profit_2"),
        _row(3, "A", "t1", 2.0, "trailing_stop"),
        # single-row loser
        _row(4, "B", "t2", -4.0),
        # fee-flipped: gross +0.01, net -0.01
        _row(5, "C", "t3", -0.01, gross=0.01),
        # phantom close
        _row(6, "D", "t4", -0.01, "reconcile_stale"),
        # TP1 leg of a position still open
        _row(7, "E", "t5", 8.0, "take_profit_1"),
    ]
    sm = win_rate_summary(rows, {("E", "live", "t5")})
    assert (sm["position_n"], sm["position_wins"]) == (3, 1)          # A wins; B, C lose
    assert sm["position_win_rate"] == pytest.approx(1 / 3)
    assert (sm["row_n"], sm["row_wins"]) == (7, 4)                    # legs inflate it
    assert sm["row_win_rate"] > sm["position_win_rate"]
    assert sm["excluded_bookkeeping_rows"] == 1
    assert sm["excluded_open_positions"] == 1
    assert sm["fee_flipped_positions"] == 1
    assert sm["note"] == REBASELINE_NOTE


def test_format_block_labels_both_numbers_and_has_rebaseline_note():
    text = "\n".join(format_win_rate_block(win_rate_summary([_row(1, "A", "t", 1.0)]), label="live"))
    assert "POSITION-level (TARGET" in text and "ROW-level (legacy" in text
    assert "Re-baseline note: Target is position-level." in text
    assert "94-95% claims must be re-validated" in text


def test_python_collapse_matches_storage_closed_positions(tmp_path):
    """Storage.closed_positions (SQL) and collapse_to_positions must agree."""
    s = Storage(path=tmp_path / "t.db")
    s.open_position(symbol="AAAUSDT", mode="live", qty=3.0, entry_price=100.0, agents=[])
    for px, reason in ((108.0, "take_profit_1"), (90.0, "stop_loss"), (95.0, "stop_loss")):
        s.reduce_position(symbol="AAAUSDT", mode="live", qty=1.0, exit_price=px,
                          exit_reason=reason, actual_exit_fee_usdt=0.02)
    s.open_position(symbol="BBBUSDT", mode="live", qty=2.0, entry_price=50.0, agents=[])
    s.reduce_position(symbol="BBBUSDT", mode="live", qty=1.0, exit_price=60.0,
                      exit_reason="take_profit_1", actual_exit_fee_usdt=0.02)  # still open
    s.open_position(symbol="CCCUSDT", mode="live", qty=1.0, entry_price=10.0, agents=[])
    s.close_position(symbol="CCCUSDT", mode="live", exit_price=10.0, exit_reason="reconcile_stale")
    s.open_position(symbol="DDDUSDT", mode="live", qty=1.0, entry_price=10.0, agents=[])
    s.close_position(symbol="DDDUSDT", mode="live", exit_price=12.0, exit_reason="take_profit_2")

    sql_positions = {(p["symbol"], p["entry_ts"]): p["pnl"] for p in s.closed_positions(limit=100, mode="live")}
    py_positions = {
        (p["symbol"], p["entry_ts"]): p["pnl"]
        for p in collapse_to_positions(s.closed_trades(limit=100), open_position_keys(s.all_positions()))
    }
    assert set(sql_positions) == set(py_positions) and len(sql_positions) == 2  # AAA, DDD
    for k, v in sql_positions.items():
        assert py_positions[k] == pytest.approx(v)


def test_loss_streak_counts_positions_not_legs(tmp_path, monkeypatch):
    """A position that banked a TP1 leg then stopped out net-negative is ONE
    losing position; per-leg counting hid it behind the winning leg."""
    from app.trading import risk_manager as rm

    s = Storage(path=tmp_path / "t.db")
    for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        s.open_position(symbol=sym, mode="live", qty=2.0, entry_price=100.0, agents=[])
        s.reduce_position(symbol=sym, mode="live", qty=1.0, exit_price=108.0,
                          exit_reason="take_profit_1", actual_exit_fee_usdt=0.02)
        s.reduce_position(symbol=sym, mode="live", qty=1.0, exit_price=90.0,
                          exit_reason="stop_loss", actual_exit_fee_usdt=0.02)

    class _S:
        loss_streak_pause_count = 3
        loss_streak_pause_minutes = 60

    monkeypatch.setattr(rm, "get_settings", lambda: _S())
    monkeypatch.setattr(rm, "storage", s)
    cooled, why = rm.RiskManager()._loss_cooldown_active("live")
    assert cooled is True and "streak=3" in why


async def test_win_rate_api_route_returns_both_labelled_definitions(monkeypatch):
    from fastapi import HTTPException

    from app.api import routes

    monkeypatch.setattr(
        routes, "win_rate_summary_from_storage",
        lambda mode=None: win_rate_summary(
            [_row(1, "A", "t1", 5.0), _row(2, "A", "t1", -1.0, "stop_loss"), _row(3, "B", "t2", -2.0)]
        ),
    )
    body = await routes.win_rate()
    assert body["position"]["n"] == 2 and body["position"]["wins"] == 1
    assert body["row"]["n"] == 3 and body["row"]["wins"] == 1
    assert "TARGET" in body["position"]["label"] and "legacy" in body["row"]["label"]
    assert body["configured_definition"] == "position"
    assert "re-validated" in body["note"]
    with pytest.raises(HTTPException):
        await routes.win_rate(mode="bogus")

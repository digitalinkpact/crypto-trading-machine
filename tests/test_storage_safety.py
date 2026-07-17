"""Tests for storage-level money-safety guards.

Covers the atomic non-negative paper debit and the cross-process tick mutex —
the fixes for the historical duplicate-order / negative-balance corruption.
"""
from __future__ import annotations

from app.storage.db import Storage


def _fresh_storage(tmp_path) -> Storage:
    return Storage(path=tmp_path / "t.db")


def test_paper_debit_never_negative(tmp_path):
    s = _fresh_storage(tmp_path)
    s.paper_reset(starting_usdt=0.0)
    s.paper_balance_add("ARB", 100.0)

    debited = s.paper_balance_debit("ARB", 30.0)
    assert debited == 30.0
    assert s.paper_balance_get("ARB") == 70.0


def test_paper_debit_clamps_to_available(tmp_path):
    s = _fresh_storage(tmp_path)
    s.paper_reset(starting_usdt=0.0)
    s.paper_balance_add("ARB", 50.0)

    # Ask for more than we hold — should only debit what's there, never go below 0.
    debited = s.paper_balance_debit("ARB", 8211.0)
    assert debited == 50.0
    assert s.paper_balance_get("ARB") == 0.0


def test_paper_debit_missing_asset(tmp_path):
    s = _fresh_storage(tmp_path)
    s.paper_reset(starting_usdt=0.0)
    assert s.paper_balance_debit("NOPE", 10.0) == 0.0
    assert s.paper_balance_get("NOPE") == 0.0


def test_tick_lock_mutual_exclusion(tmp_path):
    s = _fresh_storage(tmp_path)
    assert s.try_acquire_lock("autopilot_tick", ttl_seconds=300, owner="proc-a") is True
    # A different owner cannot acquire while it's held.
    assert s.try_acquire_lock("autopilot_tick", ttl_seconds=300, owner="proc-b") is False
    # Releasing as the wrong owner is a no-op.
    s.release_lock("autopilot_tick", owner="proc-b")
    assert s.try_acquire_lock("autopilot_tick", ttl_seconds=300, owner="proc-b") is False
    # Correct owner releases; now another process can take it.
    s.release_lock("autopilot_tick", owner="proc-a")
    assert s.try_acquire_lock("autopilot_tick", ttl_seconds=300, owner="proc-b") is True


def test_tick_lock_expires(tmp_path):
    s = _fresh_storage(tmp_path)
    assert s.try_acquire_lock("autopilot_tick", ttl_seconds=-1, owner="proc-a") is True
    # Lease already expired → another owner can acquire.
    assert s.try_acquire_lock("autopilot_tick", ttl_seconds=300, owner="proc-b") is True


def test_positions_are_isolated_by_mode(tmp_path):
    s = _fresh_storage(tmp_path)

    s.open_position(symbol="BTCUSDT", mode="paper", qty=1.0, entry_price=100.0, agents=[])
    s.open_position(symbol="BTCUSDT", mode="live", qty=2.0, entry_price=200.0, agents=[])

    positions = sorted(s.all_positions(), key=lambda p: p["mode"])
    assert len(positions) == 2
    assert positions[0]["mode"] == "live"
    assert positions[0]["qty"] == 2.0
    assert positions[1]["mode"] == "paper"
    assert positions[1]["qty"] == 1.0

    closed = s.close_position(symbol="BTCUSDT", mode="live", exit_price=250.0)
    assert closed is not None
    remaining = s.all_positions()
    assert len(remaining) == 1
    assert remaining[0]["mode"] == "paper"

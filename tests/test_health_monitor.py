"""Tests for the health-monitor auto-recovery escalation ladder in
app/trading/health.py — duplicate/failed-order detection and the
emergency-halt trigger/auto-clear flag consulted by Autopilot.tick().
"""
from __future__ import annotations

import app.trading.health as health


def setup_function(_fn) -> None:
    # Each check/escalation test starts from a clean streak/kv state so tests
    # don't leak into each other via the module-level counters.
    health._FAIL_STREAKS.clear()
    health._HEALTHY_STREAK = 0


def test_check_duplicate_orders_detects_same_symbol_side_within_window(monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    orders = [
        {"ts": now, "mode": "live", "symbol": "BTCUSDT", "side": "BUY"},
        {"ts": now, "mode": "live", "symbol": "BTCUSDT", "side": "BUY"},
    ]
    monkeypatch.setattr(health.storage, "recent_orders", lambda limit=50: orders)

    dup, detail = health._check_duplicate_orders()
    assert dup is True
    assert "BTCUSDT" in detail


def test_check_duplicate_orders_ignores_different_symbols(monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    orders = [
        {"ts": now, "mode": "live", "symbol": "BTCUSDT", "side": "BUY"},
        {"ts": now, "mode": "live", "symbol": "ETHUSDT", "side": "BUY"},
    ]
    monkeypatch.setattr(health.storage, "recent_orders", lambda limit=50: orders)

    dup, _detail = health._check_duplicate_orders()
    assert dup is False


def test_check_failed_orders_counts_exchange_rejections_only(monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"ts": now, "final_outcome": "rejected: Binance -2010 insufficient balance"},
        {"ts": now, "final_outcome": "rejected: Binance -1013 min notional"},
        {"ts": now, "final_outcome": "rejected: Binance timeout"},
        # Deliberate gate rejections are NOT failures — must not count.
        {"ts": now, "final_outcome": "rejected: risk_manager"},
        {"ts": now, "final_outcome": "rejected: signal_confidence"},
    ]
    monkeypatch.setattr(health.storage, "recent_trade_audit", lambda limit=300: rows)

    failed, count = health._check_failed_orders()
    assert count == 3
    assert failed is True  # default health_order_failure_max is 3


def test_check_failed_orders_ignores_old_entries_outside_lookback(monkeypatch):
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    rows = [{"ts": old, "final_outcome": "rejected: Binance -2010"}] * 5
    monkeypatch.setattr(health.storage, "recent_trade_audit", lambda limit=300: rows)

    failed, count = health._check_failed_orders()
    assert count == 0
    assert failed is False


def test_emergency_halt_trigger_and_clear_roundtrip(monkeypatch):
    kv_state: dict = {}
    monkeypatch.setattr(health.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(health.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    assert not kv_state.get("emergency_halt", {}).get("active")

    health._trigger_emergency_halt("binance_alive unhealthy for 3 consecutive checks")
    assert kv_state["emergency_halt"]["active"] is True
    assert "binance_alive" in kv_state["emergency_halt"]["reason"]

    # Triggering again while already active must not overwrite the reason/since.
    first_since = kv_state["emergency_halt"]["since"]
    health._trigger_emergency_halt("a different reason")
    assert kv_state["emergency_halt"]["since"] == first_since

    health._maybe_clear_emergency_halt()
    assert kv_state["emergency_halt"]["active"] is False
    assert "cleared_at" in kv_state["emergency_halt"]


def test_maybe_clear_emergency_halt_noop_when_not_active(monkeypatch):
    kv_state: dict = {"emergency_halt": {"active": False}}
    monkeypatch.setattr(health.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    set_calls = []
    monkeypatch.setattr(health.storage, "kv_set", lambda key, value: set_calls.append((key, value)))

    health._maybe_clear_emergency_halt()
    assert set_calls == []  # nothing to clear — must not write


def test_check_stale_price_flags_connected_but_no_recent_messages(monkeypatch):
    monkeypatch.setattr(
        health.live_prices, "status",
        lambda: {"connected": True, "last_msg_age_s": 999999},
    )
    stale, detail = health._check_stale_price()
    assert stale is True
    assert "stale" in detail


def test_check_stale_price_ok_when_recent(monkeypatch):
    monkeypatch.setattr(
        health.live_prices, "status",
        lambda: {"connected": True, "last_msg_age_s": 1.0},
    )
    stale, _detail = health._check_stale_price()
    assert stale is False


def test_check_duplicate_positions_detects_same_symbol_mode(monkeypatch):
    monkeypatch.setattr(
        health.storage, "all_positions",
        lambda: [
            {"symbol": "BTCUSDT", "mode": "live"},
            {"symbol": "BTCUSDT", "mode": "live"},
        ],
    )
    dup, detail = health._check_duplicate_positions()
    assert dup is True
    assert "BTCUSDT" in detail


def test_check_duplicate_positions_ok_across_modes(monkeypatch):
    # Same symbol in live vs paper is fine — they're independent books.
    monkeypatch.setattr(
        health.storage, "all_positions",
        lambda: [
            {"symbol": "BTCUSDT", "mode": "live"},
            {"symbol": "BTCUSDT", "mode": "paper"},
        ],
    )
    dup, _detail = health._check_duplicate_positions()
    assert dup is False


async def test_retry_async_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr(health.asyncio, "sleep", lambda *_a, **_k: _immediate())
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    ok, result = await health._retry_async(_flaky, attempts=3, base_delay=0.01, label="test")
    assert ok is True
    assert result == "ok"
    assert calls["n"] == 3


async def test_retry_async_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(health.asyncio, "sleep", lambda *_a, **_k: _immediate())

    async def _always_fails():
        raise ConnectionError("still down")

    ok, result = await health._retry_async(_always_fails, attempts=2, base_delay=0.01, label="test")
    assert ok is False
    assert result is None


async def test_attempt_recovery_verified_success(monkeypatch):
    monkeypatch.setattr(health.asyncio, "sleep", lambda *_a, **_k: _immediate())
    recovered_flag = {"ok": False}

    def _recover():
        recovered_flag["ok"] = True

    ok = await health._attempt_recovery(
        recover_fn=_recover, verify_fn=lambda: recovered_flag["ok"], label="test recovery",
    )
    assert ok is True


async def test_attempt_recovery_still_broken_after_attempt(monkeypatch):
    monkeypatch.setattr(health.asyncio, "sleep", lambda *_a, **_k: _immediate())

    ok = await health._attempt_recovery(
        recover_fn=lambda: None, verify_fn=lambda: False, label="test recovery",
    )
    assert ok is False


async def test_attempt_recovery_never_raises_when_recover_fn_throws(monkeypatch):
    monkeypatch.setattr(health.asyncio, "sleep", lambda *_a, **_k: _immediate())

    def _boom():
        raise RuntimeError("recover blew up")

    ok = await health._attempt_recovery(recover_fn=_boom, verify_fn=lambda: True, label="test")
    assert ok is False


async def _immediate():
    return None

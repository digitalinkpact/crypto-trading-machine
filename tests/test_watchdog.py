"""Tests for the watchdog engine (app/trading/watchdog.py) — the emergency-halt
escalation ladder consulted by Autopilot.tick(), and the public
trigger_emergency_halt() entry point other modules (e.g. Autopilot's
order-outcome-unknown protocol) use to escalate into the same halt mechanism.
"""
from __future__ import annotations

import app.trading.watchdog as watchdog


def setup_function(_fn) -> None:
    # Each test starts from a clean streak state so tests don't leak into
    # each other via the module-level counters.
    watchdog._FAIL_STREAKS.clear()
    watchdog._HEALTHY_STREAK = 0


def test_emergency_halt_trigger_and_clear_roundtrip(monkeypatch):
    kv_state: dict = {}
    monkeypatch.setattr(watchdog.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(watchdog.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    assert not kv_state.get("emergency_halt", {}).get("active")

    watchdog.trigger_emergency_halt("binance_alive unhealthy for 3 consecutive checks")
    assert kv_state["emergency_halt"]["active"] is True
    assert "binance_alive" in kv_state["emergency_halt"]["reason"]
    assert kv_state["emergency_halt"]["level"] == "new_entries_blocked"

    # Triggering again while already active must not overwrite the reason/since.
    first_since = kv_state["emergency_halt"]["since"]
    watchdog.trigger_emergency_halt("a different reason")
    assert kv_state["emergency_halt"]["since"] == first_since

    watchdog._maybe_clear_emergency_halt()
    assert kv_state["emergency_halt"]["active"] is False
    assert "cleared_at" in kv_state["emergency_halt"]


def test_emergency_halt_trigger_with_order_outcome_unknown_level(monkeypatch):
    kv_state: dict = {}
    monkeypatch.setattr(watchdog.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(watchdog.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    watchdog.trigger_emergency_halt(
        "order outcome unknown for BTCUSDT BUY coid=abc123", level="order_outcome_unknown",
    )
    assert kv_state["emergency_halt"]["level"] == "order_outcome_unknown"

    watchdog._maybe_clear_emergency_halt()
    assert kv_state["emergency_halt"]["active"] is True


def test_maybe_clear_emergency_halt_noop_when_not_active(monkeypatch):
    kv_state: dict = {"emergency_halt": {"active": False}}
    monkeypatch.setattr(watchdog.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    set_calls = []
    monkeypatch.setattr(watchdog.storage, "kv_set", lambda key, value: set_calls.append((key, value)))

    watchdog._maybe_clear_emergency_halt()
    assert set_calls == []  # nothing to clear — must not write


def test_tick_protection_engage_and_clear(monkeypatch):
    kv_state: dict = {}
    monkeypatch.setattr(watchdog.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(watchdog.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    watchdog.engage_tick_protection("trade_loop_stale", detail="age=999s")
    assert kv_state["tick_protection"]["active"] is True
    assert kv_state["tick_protection"]["reason"] == "trade_loop_stale"
    assert kv_state["tick_protection"]["detail"] == "age=999s"

    watchdog._maybe_clear_tick_protection()
    assert kv_state["tick_protection"]["active"] is False
    assert "cleared_at" in kv_state["tick_protection"]


def test_record_component_heartbeats_writes_all_components(monkeypatch):
    calls: list[tuple[str, bool, dict]] = []

    def _record(*, component: str, healthy: bool, detail: dict):
        calls.append((component, healthy, detail))

    monkeypatch.setattr(watchdog.storage, "record_component_heartbeat", _record)
    status = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "scheduler_alive": True,
        "websocket_alive": True,
        "stale_price": False,
        "risk_loop_alive": True,
        "trade_loop_alive": False,
        "binance_alive": True,
        "actions": ["x"],
        "emergency_halt": True,
        "emergency_halt_level": "new_entries_blocked",
    }

    watchdog._record_component_heartbeats(status)

    names = {name for name, _healthy, _detail in calls}
    assert names == {
        "scheduler",
        "websocket",
        "watchdog",
        "risk_manager",
        "tick_execution",
        "api_connectivity",
    }
    tick = [c for c in calls if c[0] == "tick_execution"][0]
    assert tick[1] is False


async def test_verify_safe_to_resume_fails_when_open_orders_raises(monkeypatch):
    async def _bad_snapshot(mode):
        return {"usdt_cash": "10", "total_usdt": "10"}

    monkeypatch.setattr("app.trading.portfolio.portfolio_snapshot", _bad_snapshot)
    monkeypatch.setattr(watchdog.storage, "all_positions", lambda: [])

    class _BoomClient:
        async def open_orders(self):
            raise ConnectionError("still down")

    monkeypatch.setattr(watchdog, "BinanceUSClient", lambda: _BoomClient())

    safe, detail = await watchdog._verify_safe_to_resume("live")
    assert safe is False
    assert "open-order verification failed" in detail

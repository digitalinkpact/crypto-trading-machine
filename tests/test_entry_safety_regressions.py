"""Capital limits must still hold at submission and after delayed retries."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.config import get_settings
from app.exchange import OrderSide
from app.signals import SignalAction
from app.storage.db import Storage
from app.trading import autopilot as module
from app.trading import risk as risk_module
from app.trading import risk_manager as manager_module
from app.trading.autopilot import Autopilot


@pytest.fixture
def live_guard(monkeypatch):
    settings = get_settings().model_copy(update={
        "live_buys_enabled": True,
        "aggressive_position_pct": 0.06,
        "aggressive_max_open_positions": 12,
        "max_long_exposure_pct": 0.60,
        "risk_per_trade_pct": 0.01,
        "kelly_fraction_cap": 0.25,
        "drawdown_circuit_breaker_pct": 0.15,
        "daily_loss_limit_enabled": True,
        "daily_loss_limit_pct": 0.05,
        "correlation_gate_enabled": True,
        "correlated_symbol_groups": (("BTCUSDT", "ETHUSDT", "SOLUSDT"),),
        "max_correlated_exposure_pct": 0.35,
    })
    for target in (module, risk_module, manager_module):
        monkeypatch.setattr(target, "get_settings", lambda: settings)
    kv = {
        "drawdown_halt": {"active": False},
        "emergency_halt": {"active": False},
        "tick_protection": {"active": False},
        "entry_status": {"entry_halted": False},
    }
    monkeypatch.setattr(module.storage, "kv_get", lambda key: kv.get(key))
    monkeypatch.setattr(module.storage, "all_positions", lambda: [])
    monkeypatch.setattr(module.storage, "closed_trades", lambda limit=100: [])
    monkeypatch.setattr(module.storage, "daily_realized_pnl", lambda **kw: Decimal("0"))
    monkeypatch.setattr(module.filters, "_loaded", True)
    monkeypatch.setattr(module.filters, "is_listed", lambda symbol: True)
    monkeypatch.setattr(module.filters, "meets_min", lambda *args: True)
    monkeypatch.setattr(Autopilot, "_aggressive_mode_active", lambda self: (True, "test"))
    monkeypatch.setattr(Autopilot, "_price", lambda *args: _result(Decimal("100")))
    ap = Autopilot()
    ap.state.mode = "live"
    ap.state.running = True
    ap.state.starting_balance_usdt = Decimal("1000")
    snapshot = {"usdt_cash": Decimal("410"), "total_usdt": Decimal("1000")}
    monkeypatch.setattr(module, "portfolio_snapshot", lambda **kw: _result(snapshot))
    return ap, snapshot, kv


async def _result(value):
    return value


@pytest.mark.parametrize("qty,allowed", [("0.10", True), ("0.11", False)])
async def test_final_guard_enforces_post_trade_exposure(live_guard, qty, allowed):
    ap, _, _ = live_guard
    ok, reason, detail = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal(qty)
    )
    assert ok is allowed
    assert reason == ("ok" if allowed else "order_exceeds_risk_budget")
    assert Decimal(detail["max_order_notional"]) == Decimal("10")


async def test_final_guard_rechecks_actual_drawdown(live_guard):
    ap, snapshot, _ = live_guard
    snapshot.update(usdt_cash=Decimal("800"), total_usdt=Decimal("800"))
    ok, reason, _ = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("0.1")
    )
    assert not ok
    assert reason == "drawdown_circuit_breaker"


async def test_final_guard_rechecks_daily_loss(live_guard, monkeypatch):
    ap, _, _ = live_guard
    monkeypatch.setattr(module.storage, "daily_realized_pnl", lambda **kw: Decimal("-51"))
    ok, reason, _ = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("0.1")
    )
    assert not ok
    assert reason == "daily_loss_limit"


async def test_final_guard_blocks_pyramid_beyond_total_position_cap(live_guard, monkeypatch):
    ap, snapshot, _ = live_guard
    snapshot["usdt_cash"] = Decimal("900")
    monkeypatch.setattr(module.storage, "all_positions", lambda: [
        {"symbol": "BTCUSDT", "mode": "live", "qty": "0.6", "entry_price": "100"},
    ])
    ok, reason, _ = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("0.01")
    )
    assert not ok
    assert reason.startswith("risk_manager:")


async def test_final_guard_blocks_new_dependency_halt(live_guard):
    ap, _, kv = live_guard
    kv["ml_gate_unavailable"] = {"active": True, "reason": "model unavailable"}
    ok, reason, _ = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("0.1")
    )
    assert not ok
    assert reason.startswith("ml_gate_unavailable:")


async def test_final_guard_rechecks_basket_after_other_symbol_fills(live_guard, monkeypatch):
    ap, snapshot, _ = live_guard
    snapshot["usdt_cash"] = Decimal("600")
    monkeypatch.setattr(module.storage, "all_positions", lambda: [
        {"symbol": "ETHUSDT", "mode": "live", "qty": "3.5", "entry_price": "100"},
    ])
    ok, reason, _ = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("0.1")
    )
    assert not ok
    assert reason.startswith("correlation_gate:")


async def test_basket_gate_includes_existing_position_when_pyramiding(live_guard, monkeypatch):
    ap, _, _ = live_guard
    positions = [{"symbol": "BTCUSDT", "mode": "live", "qty": "3.5"}]
    ok, reason = await ap._correlation_basket_gate(
        "BTCUSDT", Decimal("10"), positions, Decimal("1000")
    )
    assert not ok
    assert "36.0%" in reason


async def test_buy_lock_serializes_symbols_even_in_same_process(monkeypatch, tmp_path):
    store = Storage(path=tmp_path / "locks.db")
    monkeypatch.setattr(module, "storage", store)
    ap = Autopilot()
    ap.state.mode = "live"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def submit(symbol, side, *args, **kwargs):
        calls.append((symbol, side))
        if side is OrderSide.BUY:
            entered.set()
            await release.wait()
        return None

    monkeypatch.setattr(ap, "_submit_locked", submit)
    first = asyncio.create_task(ap._submit("BTCUSDT", OrderSide.BUY, Decimal("1"), []))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await ap._submit("ETHUSDT", OrderSide.BUY, Decimal("1"), []) is None
        await ap._submit("ETHUSDT", OrderSide.SELL, Decimal("1"), [])
        assert calls == [("BTCUSDT", OrderSide.BUY), ("ETHUSDT", OrderSide.SELL)]
    finally:
        release.set()
        await first
    await ap._submit("ETHUSDT", OrderSide.BUY, Decimal("1"), [])
    assert calls[-1] == ("ETHUSDT", OrderSide.BUY)


async def test_retry_refreshes_full_tick_and_cleans_up(monkeypatch):
    ap = Autopilot()
    ap.state.running = True
    ap.state.mode = "paper"
    settings = get_settings().model_copy(update={
        "orderbook_retry_attempts": 2, "orderbook_retry_delay_seconds": 1,
    })
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module.storage, "get_position", lambda *a, **kw: None)
    monkeypatch.setattr(module.asyncio, "sleep", lambda delay: _result(None))
    ticks = []

    async def tick():
        ticks.append(True)

    monkeypatch.setattr(ap, "tick", tick)
    monkeypatch.setattr(
        ap, "_place_buy", lambda *a, **kw: pytest.fail("retry must not submit stale signals")
    )
    key = ("BTCUSDT", SignalAction.BUY.value)
    task = asyncio.create_task(ap._retry_orderbook_recheck(symbol="BTCUSDT"))
    ap._orderbook_retry_tasks[key] = task
    await task
    assert len(ticks) == 2
    assert key not in ap._orderbook_retry_tasks


@pytest.mark.parametrize("breaker_tripped", [False, True])
async def test_retry_recomputes_signals_and_drawdown_through_real_tick(
    monkeypatch, tmp_path, breaker_tripped
):
    store = Storage(path=tmp_path / "retry.db")
    monkeypatch.setattr(module, "storage", store)
    settings = get_settings().model_copy(update={
        "orderbook_retry_attempts": 1,
        "orderbook_retry_delay_seconds": 1,
        "llm_in_trading_loop": False,
    })
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module.asyncio, "sleep", lambda delay: _result(None))
    monkeypatch.setattr(module.filters, "load", lambda: _result(None))
    monkeypatch.setattr(module.filters, "_loaded", True)
    refreshed_signals = {"BTCUSDT": SignalAction.HOLD}
    monkeypatch.setattr(
        module, "run_all_agents", lambda **kw: _result(refreshed_signals)
    )
    ap = Autopilot()
    ap.state.running = True
    ap.state.mode = "live"
    ap.state.starting_balance_usdt = Decimal("1000")
    monkeypatch.setattr(ap, "_check_circuit_breaker", lambda: _result(breaker_tripped))
    executed = []

    async def execute(signals, *, allow_buys):
        executed.append((signals, allow_buys))

    monkeypatch.setattr(ap, "_execute", execute)
    await ap._retry_orderbook_recheck(symbol="BTCUSDT")
    assert executed == [(refreshed_signals, not breaker_tripped)]
    assert store.kv_get("entry_status")["entry_halted"] is breaker_tripped


@pytest.mark.parametrize("change", ["stopped", "mode_changed", "already_held"])
async def test_retry_abandons_obsolete_request(monkeypatch, change):
    ap = Autopilot()
    ap.state.running = True
    ap.state.mode = "paper"

    async def wait(delay):
        if change == "stopped":
            ap.state.running = False
        elif change == "mode_changed":
            ap.state.mode = "live"

    monkeypatch.setattr(module.asyncio, "sleep", wait)
    monkeypatch.setattr(
        module.storage, "get_position",
        lambda *a, **kw: {"qty": 1} if change == "already_held" else None,
    )
    monkeypatch.setattr(ap, "tick", lambda: pytest.fail("obsolete retry must not tick"))
    await ap._retry_orderbook_recheck(symbol="BTCUSDT")

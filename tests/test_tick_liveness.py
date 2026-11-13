"""Tick liveness must track completed evaluations, not trade outcomes.

A tick whose qualifying signal is legitimately rejected by a protection gate is
a healthy tick (status BLOCKED). The watchdog must not treat it as a stalled
trade loop, otherwise it engages an emergency halt that blocks every future BUY.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.signals import Signal, SignalAction
from app.trading import autopilot as autopilot_module
from app.trading import health, watchdog
from app.trading.autopilot import Autopilot


def _qualifying_buy() -> Signal:
    from app.config import Timeframe

    return Signal(
        agent="profitstream_strategy", symbol="ETHUSDT", timeframe=Timeframe.D1,
        action=SignalAction.BUY, confidence=0.85, quality_score=85, rationale="test",
    )


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(autopilot_module, "filters", SimpleNamespace(load=AsyncMock(), loaded=True))
    pilot = Autopilot()
    pilot.state.running = True
    pilot.state.starting_balance_usdt = 1000.0
    monkeypatch.setattr(pilot, "_check_circuit_breaker", AsyncMock(return_value=False))
    return pilot


async def _run_tick_with_rejected_buy(pilot, monkeypatch) -> dict:
    monkeypatch.setattr(autopilot_module, "run_all_agents", AsyncMock(return_value={"ETHUSDT": _qualifying_buy()}))

    async def reject_by_protection(signals, *, allow_buys):
        pilot._tick_debug = {"by_reason": {"breadth_gate_blocked": 1}, "per_symbol": {}}

    monkeypatch.setattr(pilot, "_execute", reject_by_protection)
    return await pilot.tick(source="scheduled")


async def _run_one_watchdog_iteration(monkeypatch, pilot) -> dict:
    store = autopilot_module.storage
    monkeypatch.setattr(watchdog, "storage", store)
    monkeypatch.setattr(watchdog, "autopilot", pilot)
    monkeypatch.setattr(watchdog, "live_prices", SimpleNamespace(connected=True))
    monkeypatch.setattr(watchdog, "_FAIL_STREAKS", {})
    monkeypatch.setattr(health, "_retry_async", AsyncMock(return_value=(True, {})))
    monkeypatch.setattr(health, "_check_stale_price", lambda: (False, ""))
    monkeypatch.setattr(health, "_check_open_orders", AsyncMock(return_value=(False, 0)))
    monkeypatch.setattr(health, "_check_portfolio_discrepancy", AsyncMock(return_value=(False, 0)))
    monkeypatch.setattr(health, "_check_memory", lambda: 100.0)
    monkeypatch.setattr(health, "_check_cpu_pct", lambda elapsed: 1.0)
    emergency_risk_run = AsyncMock()
    monkeypatch.setattr("app.trading.risk_loop._run_once", emergency_risk_run)
    monkeypatch.setattr("app.trading.reconcile.reconcile_positions", AsyncMock(return_value={}))
    monkeypatch.setattr(watchdog, "_SCHEDULER_REF", SimpleNamespace(running=True, get_jobs=lambda: [], wakeup=Mock()))
    store.kv_set("risk_loop_last_run", time.time())

    async def stop_after_one_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_one_iteration)
    with pytest.raises(asyncio.CancelledError):
        await watchdog._health_loop()
    return store.kv_get("health_status")


async def test_blocked_tick_keeps_trade_loop_alive_and_no_halt(pilot, monkeypatch):
    store = autopilot_module.storage
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    store.kv_set("autopilot_last_success", long_ago)

    result = await _run_tick_with_rejected_buy(pilot, monkeypatch)
    assert result["status"] == "BLOCKED"
    assert result["errors"] == []

    status = await _run_one_watchdog_iteration(monkeypatch, pilot)

    assert status["trade_loop_alive"] is True
    assert "tick_protection_engaged" not in status["actions"]
    assert not (store.kv_get("tick_protection") or {}).get("active")
    assert not (store.kv_get("emergency_halt") or {}).get("active")


async def test_started_scheduler_fires_autopilot_tick(monkeypatch):
    from app.scheduler import build_scheduler, jobs

    fired = asyncio.Event()

    async def fake_tick(*, source):
        fired.set()

    monkeypatch.setattr(jobs, "autopilot", SimpleNamespace(tick=fake_tick))
    scheduler = build_scheduler()
    for job in scheduler.get_jobs():
        if job.id != "autopilot":
            job.pause()
    scheduler.start()
    try:
        scheduler.get_job("autopilot").modify(next_run_time=datetime.now(timezone.utc))
        await asyncio.wait_for(fired.wait(), timeout=5)
    finally:
        scheduler.shutdown(wait=False)


async def test_risk_loop_manages_positions_while_buys_halted(monkeypatch):
    from app.trading import risk_loop

    store = autopilot_module.storage
    monkeypatch.setattr(risk_loop, "storage", store)
    watchdog.trigger_emergency_halt("test halt", level="new_entries_blocked")
    risk_gates = AsyncMock(return_value={"success": True, "positions_evaluated": 1, "errors": []})
    monkeypatch.setattr(risk_loop.autopilot, "_run_risk_gates", risk_gates)

    await risk_loop._run_once()

    risk_gates.assert_awaited_once()
    assert store.kv_get("risk_loop_last_run") is not None


async def test_tick_skipped_by_held_lock_does_not_refresh_liveness(pilot, monkeypatch):
    store = autopilot_module.storage
    assert store.try_acquire_lock("autopilot_tick", ttl_seconds=300, owner="other-process")
    run_agents = AsyncMock(return_value={})
    monkeypatch.setattr(autopilot_module, "run_all_agents", run_agents)

    result = await pilot.tick(source="scheduled")

    assert result["status"] == "SKIPPED"
    run_agents.assert_not_awaited()
    assert store.kv_get("autopilot_last_completed_tick") is None


async def test_tick_failed_in_execution_does_not_refresh_liveness(pilot, monkeypatch):
    from app.trading.execution_context import record_error

    store = autopilot_module.storage
    monkeypatch.setattr(autopilot_module, "run_all_agents", AsyncMock(return_value={"ETHUSDT": _qualifying_buy()}))

    async def failing_execute(signals, *, allow_buys):
        record_error("signal_execution", RuntimeError("boom"))

    monkeypatch.setattr(pilot, "_execute", failing_execute)
    result = await pilot.tick(source="scheduled")

    assert result["status"] == "FAILED"
    assert store.kv_get("autopilot_last_completed_tick") is None


async def test_genuinely_stalled_ticks_engage_protection(pilot, monkeypatch):
    store = autopilot_module.storage
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    store.kv_set(autopilot_module.LAST_COMPLETED_TICK_KEY, stale)
    pilot.state.last_tick_at = stale

    status = await _run_one_watchdog_iteration(monkeypatch, pilot)

    assert status["trade_loop_alive"] is False
    assert (store.kv_get("tick_protection") or {}).get("active")
    assert (store.kv_get("emergency_halt") or {}).get("active")

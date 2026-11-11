"""Manual tick API and executor outcomes, without network calls or live orders."""
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import warnings

import httpx
import pytest
from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="'crypt' is deprecated", category=DeprecationWarning)
    from app.api import routes
from app.trading import autopilot as autopilot_module
from app.trading.autopilot import Autopilot


@pytest.fixture
def tick_api(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    tick = AsyncMock(return_value={"status": "executed", "orders_submitted": 0, "reasons": []})
    monkeypatch.setattr(routes, "autopilot", SimpleNamespace(state=SimpleNamespace(mode="live"), tick=tick))
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(paper_trading=False, live_mode=True))
    return app, tick


async def test_manual_tick_requires_session_even_without_global_guard(tick_api):
    app, tick = tick_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/autopilot/tick", headers={"X-Tick-Confirmation": "RUN_ONE_LIVE_TICK"})
    assert response.status_code == 401
    tick.assert_not_awaited()


@pytest.mark.parametrize("confirmation", [None, "", "RUN_ONE_PAPER_TICK", "true"])
async def test_live_tick_requires_exact_confirmation(tick_api, monkeypatch, confirmation):
    app, tick = tick_api
    monkeypatch.setattr(routes, "current_user", lambda request: {"id": 1})
    headers = {} if confirmation is None else {"X-Tick-Confirmation": confirmation}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/autopilot/tick", headers=headers)
    assert response.status_code == 400
    tick.assert_not_awaited()


@pytest.mark.parametrize("mode,paper,live,confirmation", [
    ("live", False, True, "RUN_ONE_LIVE_TICK"),
    ("paper", True, False, "RUN_ONE_PAPER_TICK"),
    ("live", True, False, "RUN_ONE_LIVE_TICK"),
    ("paper", False, True, "RUN_ONE_LIVE_TICK"),
])
async def test_confirmed_tick_uses_existing_instance(tick_api, monkeypatch, mode, paper, live, confirmation):
    app, tick = tick_api
    routes.autopilot.state.mode = mode
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(paper_trading=paper, live_mode=live))
    monkeypatch.setattr(routes, "current_user", lambda request: {"id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/autopilot/tick", headers={"X-Tick-Confirmation": confirmation})
    assert response.status_code == 200
    assert response.json() == tick.return_value
    tick.assert_awaited_once_with(source="manual", requested_by=1)


async def test_manual_tick_failure_does_not_claim_no_orders(tick_api, monkeypatch):
    app, tick = tick_api
    tick.side_effect = RuntimeError("exchange failed after submission")
    monkeypatch.setattr(routes, "current_user", lambda request: {"id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/autopilot/tick", headers={"X-Tick-Confirmation": "RUN_ONE_LIVE_TICK"})
    assert response.status_code == 500
    assert response.json()["detail"]["orders_submitted"] is None
    assert "exchange failed" not in response.text


async def test_manual_tick_rejects_cross_origin(tick_api, monkeypatch):
    app, tick = tick_api
    monkeypatch.setattr(routes, "current_user", lambda request: {"id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/autopilot/tick", headers={
            "Origin": "https://untrusted.example", "X-Tick-Confirmation": "RUN_ONE_LIVE_TICK",
        })
    assert response.status_code == 403
    tick.assert_not_awaited()


async def test_dashboard_uses_persisted_execution_and_one_portfolio_read(monkeypatch, tmp_path):
    from decimal import Decimal
    from app.config import Settings
    from app.storage.db import Storage
    from app.trading.autopilot import AutopilotState

    store = Storage(tmp_path / "dashboard.db")
    monkeypatch.setattr(routes, "storage", store)
    monkeypatch.setattr(routes, "get_settings", lambda: Settings(_env_file=None, trading_mode="simulation"))
    tick = AsyncMock()
    monkeypatch.setattr(routes, "autopilot", SimpleNamespace(state=AutopilotState(running=True), tick=tick))
    snapshot = AsyncMock(return_value={"total_usdt": Decimal("25000"), "usdt_cash": Decimal("25000"), "holdings": [], "all_balances": {"USDT": Decimal("25000")}})
    monkeypatch.setattr(routes, "portfolio_snapshot", snapshot)
    store.record_tick_run({
        "tick_id": "verified-tick", "started_at": "2026-11-11T00:00:00+00:00",
        "completed_at": "2026-11-11T00:00:01+00:00", "mode": "paper", "source": "scheduled",
        "status": "EXECUTED", "pairs_evaluated": 44, "duration_seconds": 1,
        "decisions": {"BUY": 0, "SELL": 0, "HOLD": 44}, "orders_submitted": 0,
        "orders_filled": 0, "errors": [], "risk_rejections": {},
    })
    html = await routes.dashboard()
    assert "RUN ONE TICK" in html and "verified-tick" in html
    assert "SIMULATION" in html and "EXECUTED" in html
    assert "every 15 minutes" not in html and "simulated $10,000" not in html
    snapshot.assert_awaited_once_with()
    tick.assert_not_awaited()


async def test_mode_change_is_rejected_without_writing_configuration(tick_api, monkeypatch):
    app, tick = tick_api
    writer = Mock()
    monkeypatch.setattr(routes, "save_trading_mode", writer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/settings/mode", data={"mode": "paper"})
    assert response.status_code == 409
    writer.assert_not_called()
    assert routes.autopilot.state.mode == "live"
    tick.assert_not_awaited()


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda *args: None)
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda *args: None)
    monkeypatch.setattr(autopilot_module.storage, "try_acquire_lock", lambda *args, **kwargs: True)
    release = Mock()
    monkeypatch.setattr(autopilot_module.storage, "release_lock", release)
    monkeypatch.setattr(autopilot_module.filters, "load", AsyncMock())
    monkeypatch.setattr(autopilot_module.filters, "_loaded", True)
    monkeypatch.setattr(autopilot_module.paper_exchange, "ensure_seeded", lambda: None)
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: SimpleNamespace(paper_trading=True, llm_in_trading_loop=False))
    monkeypatch.setattr(autopilot_module, "run_all_agents", AsyncMock(return_value={}))
    instance = Autopilot()
    instance.state.running = True
    monkeypatch.setattr(instance, "_save", lambda: None)
    monkeypatch.setattr(instance, "_entry_block_reason", lambda: None)
    monkeypatch.setattr(instance, "_check_circuit_breaker", AsyncMock(return_value=False))
    monkeypatch.setattr(instance, "_execute", AsyncMock())
    return instance


async def test_stopped_tick_reports_skipped(executor):
    executor.state.running = False
    assert (await executor.tick())["reasons"] == ["autopilot_stopped"]
    executor._execute.assert_not_awaited()


async def test_overlapping_tick_reports_skipped(executor):
    async with executor._lock:
        result = await executor.tick()
    assert result["status"] == "SKIPPED"
    assert result["reasons"] == ["tick_in_progress"]
    executor._execute.assert_not_awaited()


async def test_cross_process_lock_reports_skipped(executor, monkeypatch):
    monkeypatch.setattr(autopilot_module.storage, "try_acquire_lock", lambda *args, **kwargs: False)
    assert (await executor.tick())["reasons"] == ["tick_locked_by_another_process"]
    executor._execute.assert_not_awaited()


@pytest.mark.parametrize("reason,submitted,expected", [
    ("no_signals", False, "EXECUTED"),
    ("executed_buy", True, "EXECUTED"),
    ("executed_sell", False, "EXECUTED"),
])
async def test_tick_reports_only_current_execution(executor, monkeypatch, reason, submitted, expected):
    executor._tick_debug = {"per_symbol": {"stale": {"submitted": True}}}

    async def execute(signals, *, allow_buys):
        executor._persist_skip_stats(Counter({reason: 1}), {"symbol": {"submitted": submitted}}, total=0)

    monkeypatch.setattr(executor, "_execute", execute)
    result = await executor.tick()
    assert result["status"] == expected
    assert result["orders_submitted"] == 0
    assert reason in result["reasons"]


async def test_halt_does_not_bypass_execution_gates(executor, monkeypatch):
    monkeypatch.setattr(executor, "_entry_block_reason", lambda: "emergency_halt")
    monkeypatch.setattr(executor, "_check_circuit_breaker", AsyncMock(return_value=True))
    result = await executor.tick()
    assert result["status"] == "EXECUTED"
    assert "emergency_halt" in result["reasons"]
    executor._execute.assert_awaited_once_with({}, allow_buys=False)


async def test_rejected_candidate_reports_block_reason(executor, monkeypatch):
    monkeypatch.setattr(autopilot_module, "run_all_agents", AsyncMock(return_value={"candidate": object()}))

    async def execute(signals, *, allow_buys):
        executor._persist_skip_stats(Counter({"risk_cap": 1}), {}, total=1)

    monkeypatch.setattr(executor, "_execute", execute)
    result = await executor.tick()
    assert result["status"] == "BLOCKED"
    assert "risk_cap" in result["reasons"]
    assert result["orders_submitted"] == 0


async def test_agent_failure_releases_lock(executor, monkeypatch):
    release = Mock()
    monkeypatch.setattr(autopilot_module.storage, "release_lock", release)
    monkeypatch.setattr(autopilot_module, "run_all_agents", AsyncMock(side_effect=RuntimeError("offline")))
    assert (await executor.tick())["status"] == "FAILED"
    release.assert_called_once_with("autopilot_tick", owner=executor._owner)
    executor._execute.assert_not_awaited()


async def test_tick_records_completion_and_actual_order_outcomes(executor, monkeypatch):
    from app.trading.execution_context import current_execution

    async def execute(signals, *, allow_buys):
        current_execution.get()["orders"].append({"submitted": True, "filled": True, "status": "FILLED"})
        executor._persist_skip_stats(Counter({"executed_sell": 2}), {"symbol": {"submitted": True}}, total=1)

    monkeypatch.setattr(executor, "_execute", execute)
    result = await executor.tick(source="manual")
    assert result["status"] == "EXECUTED"
    assert result["orders_submitted"] == result["orders_filled"] == 1
    assert result["completed_at"] >= result["started_at"]
    assert result["duration_seconds"] >= 0
    assert result["source"] == "manual"
    persisted = autopilot_module.storage.recent_tick_runs(1)[0]
    assert persisted["tick_id"] == result["tick_id"]
    assert persisted["completed_at"] == result["completed_at"]


async def test_technical_error_is_not_hidden_by_filled_order(executor, monkeypatch):
    from app.trading.execution_context import current_execution, record_error

    async def execute(signals, *, allow_buys):
        current_execution.get()["orders"].append({"submitted": True, "filled": True})
        record_error("accounting", RuntimeError("write failed"))

    monkeypatch.setattr(executor, "_execute", execute)
    result = await executor.tick()
    assert result["status"] == "FAILED"
    assert result["orders_filled"] == 1
    assert result["errors"] == [{"stage": "accounting", "type": "RuntimeError"}]


async def test_tick_timeout_has_durable_failed_result(executor, monkeypatch):
    import asyncio

    async def timeout(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(autopilot_module.asyncio, "wait_for", timeout)
    result = await executor.tick(source="manual")
    assert result["status"] == "FAILED" and result["completed_at"]
    assert result["reasons"] == ["tick_timeout"]
    assert autopilot_module.storage.recent_tick_runs(1)[0]["status"] == "FAILED"


async def test_concurrent_ticks_share_single_executor(executor, monkeypatch):
    import asyncio

    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(signals, *, allow_buys):
        calls.append(True)
        entered.set()
        await finish.wait()

    monkeypatch.setattr(executor, "_execute", execute)
    first = asyncio.create_task(executor.tick(source="scheduled"))
    await entered.wait()
    second = await executor.tick(source="manual")
    finish.set()
    await first
    assert second["status"] == "SKIPPED" and len(calls) == 1


async def test_scheduler_callback_invokes_shared_executor(monkeypatch):
    from app.scheduler import build_scheduler, jobs

    tick = AsyncMock()
    monkeypatch.setattr(jobs, "autopilot", SimpleNamespace(tick=tick))
    scheduler = build_scheduler()
    job = scheduler.get_job("autopilot")
    assert str(job.trigger) == "cron[minute='*']"
    await job.func()
    tick.assert_awaited_once_with(source="scheduled")
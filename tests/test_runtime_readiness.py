from __future__ import annotations

import fcntl
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response

from app.api import routes
from app.exchange.ws_stream import LivePriceCache
from app.main import _app_lifespan, _process_lock, app


@pytest.fixture
def ready(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    monitor = {
        "timestamp": now, "rest_alive": True, "binance_alive": True,
        "scheduler_alive": True, "risk_loop_alive": True, "trade_loop_alive": True,
    }
    routes.storage.kv_set("health_status", monitor)
    routes.storage.kv_set("autopilot_last_completed_tick", now)
    ws = {
        "enabled": True, "connected": True, "last_msg_age_s": 1,
        "last_update_at": now, "reconnect_attempts": 2,
    }
    monkeypatch.setattr(routes.live_prices, "status", lambda: ws)
    monkeypatch.setattr(routes.autopilot.state, "running", True)
    return monitor, ws


async def test_readiness_reports_safe_mode_and_fresh_dependencies(ready):
    response = Response()
    body = await routes.health(response)
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["binance_rest_healthy"] and body["price_fresh"]
    assert body["paper_trading"] and body["dry_run"] and not body["live_mode"]
    assert body["last_successful_evaluation_at"] is not None
    assert body["websocket_reconnect_attempts"] == 2


@pytest.mark.parametrize("failure", ["rest", "stale", "disconnected", "monitor_stale", "database"])
async def test_readiness_fails_closed_without_leaking_errors(ready, monkeypatch, failure):
    monitor, ws = ready
    if failure == "rest":
        monitor["rest_alive"] = False
    elif failure == "stale":
        ws["last_msg_age_s"] = 9999
    elif failure == "disconnected":
        ws["connected"] = False
    elif failure == "monitor_stale":
        monitor["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    elif failure == "database":
        monkeypatch.setattr(routes.storage, "kv_set", Mock(side_effect=RuntimeError("secret-value")))
    if failure not in {"database", "stale", "disconnected"}:
        routes.storage.kv_set("health_status", monitor)
    response = Response()
    body = await routes.health(response)
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert "secret-value" not in str(body)


def test_invalid_websocket_frames_do_not_refresh_market_data():
    cache = LivePriceCache()
    for frame in ('[]', '{"s":"BTCUSDT","c":"NaN"}', '{"x":1}'):
        cache._ingest(frame)
    assert cache.status()["last_update_at"] is None
    cache._ingest('{"s":"BTCUSDT","c":"1"}')
    assert cache.status()["last_update_at"] is not None


def test_process_lock_blocks_other_process_and_releases(tmp_path):
    lock_path = tmp_path / ".crypto-bot.lock"
    script = (
        "import fcntl,sys; f=open(sys.argv[1], 'a'); "
        "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with _process_lock(tmp_path):
        assert subprocess.run([sys.executable, "-c", script, str(lock_path)], capture_output=True).returncode != 0
        with pytest.raises(RuntimeError, match="another crypto-bot"):
            with _process_lock(tmp_path):
                pass
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


async def test_startup_verification_failure_never_starts_loops(monkeypatch):
    from app import main

    monkeypatch.setattr(main.filters, "load", AsyncMock())
    monkeypatch.setattr(main, "verify_before_trading", AsyncMock(side_effect=RuntimeError("verification failed")))
    start = Mock()
    monkeypatch.setattr(main.live_prices, "start", start)
    with pytest.raises(RuntimeError, match="verification failed"):
        async with _app_lifespan(app):
            pytest.fail("startup must fail")
    start.assert_not_called()


async def test_lifespan_shutdown_stops_background_components(monkeypatch):
    from app import main

    monkeypatch.setattr(main.filters, "load", AsyncMock())
    monkeypatch.setattr(main, "verify_before_trading", AsyncMock())
    monkeypatch.setattr(main, "startup_report", AsyncMock())
    monkeypatch.setattr(main, "_log_price_diagnostic", AsyncMock())
    monkeypatch.setattr(main.live_prices, "start", Mock())
    ws_stop = AsyncMock()
    risk_stop = AsyncMock()
    health_stop = AsyncMock()
    monkeypatch.setattr(main.live_prices, "stop", ws_stop)
    monkeypatch.setattr(main.risk_loop, "start", Mock())
    monkeypatch.setattr(main.risk_loop, "stop", risk_stop)
    monkeypatch.setattr(main, "start_health_monitor", Mock())
    monkeypatch.setattr(main, "stop_health_monitor", health_stop)
    scheduler = SimpleNamespace(start=Mock(), running=True, get_jobs=lambda: [], shutdown=Mock())
    monkeypatch.setattr(main, "build_scheduler", lambda: scheduler)
    async with _app_lifespan(app):
        pass
    ws_stop.assert_awaited_once()
    risk_stop.assert_awaited_once()
    health_stop.assert_awaited_once()
    scheduler.shutdown.assert_called_once_with(wait=False)

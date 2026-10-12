"""Tests for the BUY-path diagnostics work (branch fix/buy-path-diagnostics).

Covers:
  * the telemetry-masking defect: a portfolio/account-data fetch failure in
    Autopilot._execute must record a skip reason instead of leaving stale
    by_reason/per_symbol telemetry in place, and
  * every new /autopilot/status diagnostic field, including missing/malformed
    data, stale ticks, and the effective (hardcoded) ML threshold.

No live orders are placed; storage and the exchange boundary are mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.trading.autopilot as autopilot_module
from app.trading.autopilot import Autopilot


# ─────────────────────────── regression: defect ────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_fetch_failure_records_skip_reason(monkeypatch):
    """A portfolio_snapshot failure must persist a `portfolio_fetch_failed` skip
    reason + sanitized exchange error, not leave stale telemetry behind."""
    ap = Autopilot()
    ap.state.mode = "live"

    kv: dict = {
        # Pre-seed a prior tick's telemetry to prove it gets overwritten.
        "autopilot_last_tick_debug": {"by_reason": {"low_confidence": 5}, "per_symbol": {}},
    }
    monkeypatch.setattr(autopilot_module.storage, "kv_set",
                        lambda key, value: kv.__setitem__(key, value), raising=True)
    monkeypatch.setattr(autopilot_module.storage, "kv_get",
                        lambda key, default=None: kv.get(key, default), raising=True)

    async def _boom(*, mode):
        raise RuntimeError("binance 503 Service Unavailable")

    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _boom)

    class _BuySig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.82
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _BuySig()}, allow_buys=True)

    debug = kv["autopilot_last_tick_debug"]
    assert debug["by_reason"] == {"portfolio_fetch_failed": 1}
    assert kv["autopilot_skip_stats"] == {"portfolio_fetch_failed": 1}
    assert kv["autopilot_last_exchange_error"]["error"].startswith("RuntimeError")
    assert "503" in kv["autopilot_last_exchange_error"]["error"]
    assert "portfolio fetch failed" in ap.state.last_error


@pytest.mark.asyncio
async def test_successful_snapshot_records_account_ok(monkeypatch):
    """A successful snapshot records a last-account-ok timestamp."""
    ap = Autopilot()
    ap.state.mode = "paper"

    kv: dict = {}
    monkeypatch.setattr(autopilot_module.storage, "kv_set",
                        lambda key, value: kv.__setitem__(key, value), raising=True)
    monkeypatch.setattr(autopilot_module.storage, "kv_get",
                        lambda key, default=None: kv.get(key, default), raising=True)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [], raising=True)
    monkeypatch.setattr(Autopilot, "_count_non_dust_positions",
                        lambda *_a, **_k: __import__("asyncio").sleep(0, result=(0, set())),
                        raising=True)

    async def _snap(*, mode):
        return {"usdt_cash": Decimal("100"), "total_usdt": Decimal("100"), "all_balances": {}}

    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snap)

    # Stop the tick right after the account check so we only exercise that path.
    monkeypatch.setattr(autopilot_module.online_regime, "threshold_delta",
                        lambda: (_ for _ in ()).throw(StopIteration), raising=False)

    class _HoldSig:
        action = autopilot_module.SignalAction.HOLD
        confidence = 0.1
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    try:
        await ap._execute({"BTCUSDT": _HoldSig()}, allow_buys=True)
    except Exception:
        pass  # downstream gate logic is out of scope; we only assert the account check

    assert "autopilot_last_account_ok" in kv


# ─────────────────────────── /autopilot/status ─────────────────────────────

@pytest.fixture
def _status_env(monkeypatch):
    """Drive app.api.routes.autopilot_status with controlled storage + state."""
    import app.api.routes as routes

    kv: dict = {}
    monkeypatch.setattr(routes.storage, "kv_get",
                        lambda key, default=None: kv.get(key, default), raising=True)
    monkeypatch.setattr(routes.storage, "equity_curve", lambda limit=1: [], raising=True)
    monkeypatch.setattr(routes, "credentials_present", lambda: True, raising=True)

    st = routes.autopilot.state
    monkeypatch.setattr(st, "running", True, raising=False)
    monkeypatch.setattr(st, "mode", "live", raising=False)
    monkeypatch.setattr(st, "last_error", "", raising=False)
    return routes, kv, st


@pytest.mark.asyncio
async def test_status_is_backward_compatible(_status_env):
    routes, kv, st = _status_env
    out = await routes.autopilot_status()
    for key in (
        "running", "mode", "credentials_set", "started_at", "last_tick_at",
        "trades_executed", "last_action", "last_error",
        "entry_halted", "entry_halted_reasons", "system_offline",
    ):
        assert key in out


@pytest.mark.asyncio
async def test_status_reports_new_diagnostic_fields(_status_env):
    routes, kv, st = _status_env
    out = await routes.autopilot_status()
    for key in (
        "last_tick_age_seconds", "tick_stale", "stale_tick_threshold_seconds",
        "last_tick", "ml_gate", "risk", "exposure",
        "last_account_ok", "last_exchange_error", "commit",
    ):
        assert key in out
    # Effective (hardcoded) ML gate thresholds are surfaced honestly.
    assert out["ml_gate"]["effective_buy_threshold"] == 0.40
    assert out["ml_gate"]["effective_sell_threshold"] == 0.50
    assert "configured_threshold_unused" in out["ml_gate"]


@pytest.mark.asyncio
async def test_status_tick_stale_true_when_old(monkeypatch, _status_env):
    routes, kv, st = _status_env
    monkeypatch.setattr(st, "last_tick_at",
                        datetime.now(timezone.utc) - timedelta(hours=2), raising=False)
    out = await routes.autopilot_status()
    assert out["tick_stale"] is True
    assert out["last_tick_age_seconds"] > out["stale_tick_threshold_seconds"]


@pytest.mark.asyncio
async def test_status_tick_stale_false_when_fresh(monkeypatch, _status_env):
    routes, kv, st = _status_env
    monkeypatch.setattr(st, "last_tick_at", datetime.now(timezone.utc), raising=False)
    out = await routes.autopilot_status()
    assert out["tick_stale"] is False
    assert out["last_tick_age_seconds"] < out["stale_tick_threshold_seconds"]


@pytest.mark.asyncio
async def test_status_handles_malformed_tick_debug(monkeypatch, _status_env):
    routes, kv, st = _status_env
    kv["autopilot_last_tick_debug"] = "not-a-dict"      # malformed
    kv["entry_status"] = ["also-wrong"]                 # malformed
    out = await routes.autopilot_status()
    assert out["last_tick"]["by_reason"] == {}
    assert out["last_tick"]["per_symbol"] == {}
    assert out["entry_halted"] is False


@pytest.mark.asyncio
async def test_status_surfaces_last_tick_breakdown_and_exchange_error(monkeypatch, _status_env):
    routes, kv, st = _status_env
    kv["autopilot_last_tick_debug"] = {
        "ts": "2026-10-12T21:00:00+00:00",
        "total_signals": 25,
        "by_reason": {"risk_manager": 20, "low_confidence": 5},
        "per_symbol": {"BTCUSDT": {"reason": "risk_manager"}},
    }
    kv["autopilot_last_exchange_error"] = {"ts": "2026-10-12T21:00:00+00:00",
                                           "error": "RuntimeError: binance 503"}
    out = await routes.autopilot_status()
    assert out["last_tick"]["by_reason"]["risk_manager"] == 20
    assert out["last_tick"]["per_symbol"]["BTCUSDT"]["reason"] == "risk_manager"
    assert out["last_exchange_error"]["error"].endswith("503")


@pytest.mark.asyncio
async def test_status_exposure_from_equity_curve(monkeypatch, _status_env):
    routes, kv, st = _status_env
    monkeypatch.setattr(
        routes.storage, "equity_curve",
        lambda limit=1: [{"ts": "2026-10-12T21:00:00+00:00", "total_usdt": 200.0,
                          "cash_usdt": 56.0, "invested_usdt": 144.0, "mode": "live"}],
        raising=True,
    )
    out = await routes.autopilot_status()
    assert out["exposure"]["long_exposure_pct"] == pytest.approx(0.72)
    assert out["exposure"]["usdt_cash"] == 56.0


@pytest.mark.asyncio
async def test_status_commit_from_env(monkeypatch, _status_env):
    routes, kv, st = _status_env
    monkeypatch.setenv("APP_COMMIT", "deadbeefcafe")
    out = await routes.autopilot_status()
    assert out["commit"] == "deadbeefcafe"
